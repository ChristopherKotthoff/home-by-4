"""Keewano event logging (C5). Stdlib only, best effort, never raises.

emit(name, props) always appends {ts, name, props} to data/events.jsonl. If KEEWANO_TOKEN is set it also
queues the event for a background worker that POSTs it to Keewano's REST ingest.

Keewano model (docs.keewano.com speedhacks quickstart): an event is a name plus ONE typed value attached to an
entity (installId, must be a UUID). We therefore turn one emit() into a few events: one context event per prop
(string/uint/bool, carried forward on the entity) and then the named event itself (type 'none').
Custom event types are registered via POST .../json/custom (ids 2000+, append-only). We keep the id table in
data/keewano_registry.json so ids stay stable across runs and across backfill/live.
"""
import datetime
import json
import os
import queue
import re
import threading
import time
import uuid
import zlib
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
EVENTS_PATH = os.path.join(DATA, "events.jsonl")
REGISTRY_PATH = os.path.join(DATA, "keewano_registry.json")
API = "https://api.keewano.com/event/ingress/v1/json"
FIRST_ID = 2000
UINT_MAX = 2 ** 32 - 1

_file_lock = threading.Lock()
_reg_lock = threading.Lock()
_q = queue.Queue(maxsize=5000)
_worker = None
_worker_lock = threading.Lock()
_registered_hash = None
_last_error = None


def token():
    return os.environ.get("KEEWANO_TOKEN") or None


# ---------------------------------------------------------------- helpers

def camel(s):
    parts = re.split(r"[^A-Za-z0-9]+", str(s))
    return "".join(p[:1].upper() + p[1:] for p in parts if p) or "Unnamed"


def entity_id(name, props):
    """Person events -> one entity per person; day events -> one per date; everything else -> the owner."""
    if props.get("person"):
        key = "homeby4-person-" + str(props["person"]).strip().lower()
    elif props.get("date"):
        key = "homeby4-day-" + str(props["date"])
    else:
        key = "homeby4-owner"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def to_unix(ts):
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        try:
            return int(float(ts))
        except ValueError:
            pass
        try:
            d = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            return int(d.timestamp())
        except ValueError:
            pass
    return int(time.time())


def _typed(value):
    """-> (dataType, data) or None if the value cannot be represented."""
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, int):
        return ("uint", min(value, UINT_MAX)) if value >= 0 else ("string", str(value))
    if isinstance(value, float):
        return ("uint", min(int(round(value)), UINT_MAX)) if value >= 0 else ("string", str(round(value)))
    if isinstance(value, str):
        return "string", value[:256]
    if isinstance(value, (list, tuple)) and all(isinstance(x, (str, int, float)) for x in value):
        return "string", ",".join(str(x) for x in value)[:256]
    return None  # None, dict, nested -> skipped


# ---------------------------------------------------------------- registry (append-only ids)

def _load_registry():
    try:
        with open(REGISTRY_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"eventTypes": []}


def _type_id(reg, name, dtype):
    """Find or append a custom event type; returns its eventTypeId. Caller holds _reg_lock."""
    for t in reg["eventTypes"]:
        if t["name"] == name and t["dataType"] == dtype:
            return t["eventTypeId"]
    taken = {t["name"] for t in reg["eventTypes"]}
    nm = name if name not in taken else "%s_%s" % (name, dtype)
    tid = FIRST_ID + len(reg["eventTypes"])
    reg["eventTypes"].append({"eventTypeId": tid, "name": nm, "dataType": dtype})
    return tid


def registry_hash(reg):
    return zlib.crc32(json.dumps(reg["eventTypes"], sort_keys=True).encode()) or 1


def build_events(name, props, ts):
    """-> (entity uuid, [event dicts], registry) for one emit(). Registry is persisted when it grew."""
    with _reg_lock:
        reg = _load_registry()
        n0 = len(reg["eventTypes"])
        evs = []
        for k, v in (props or {}).items():
            if k.endswith(("_shift_min", "_delta_min")) and isinstance(v, (int, float)) and not isinstance(v, bool):
                # Keewano has no signed int: keep the magnitude as uint and the direction as a label
                evs.append({"eventTypeId": _type_id(reg, camel(k), "uint"), "dataType": "uint", "timestamp": ts,
                            "data": min(abs(int(round(v))), UINT_MAX)})
                evs.append({"eventTypeId": _type_id(reg, camel(k) + "Dir", "string"), "dataType": "string",
                            "timestamp": ts, "data": "earlier" if v < 0 else "later" if v > 0 else "same"})
                continue
            tv = _typed(v)
            if tv:
                evs.append({"eventTypeId": _type_id(reg, camel(k), tv[0]), "dataType": tv[0],
                            "timestamp": ts, "data": tv[1]})
        evs.append({"eventTypeId": _type_id(reg, camel(name), "none"), "dataType": "none", "timestamp": ts})
        if len(reg["eventTypes"]) != n0:
            os.makedirs(DATA, exist_ok=True)
            tmp = REGISTRY_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(reg, f, indent=1)
            os.replace(tmp, REGISTRY_PATH)
    return entity_id(name, props or {}), evs, reg


# ---------------------------------------------------------------- HTTP

def _post(path, body, tok, timeout=8):
    req = urllib.request.Request("%s/%s" % (API, path), data=json.dumps(body).encode(), method="POST",
                                 headers={"k-token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()[:300]


def ensure_registered(reg, tok):
    """Register the full custom-event list when it changed (safe to repeat per docs)."""
    global _registered_hash
    h = registry_hash(reg)
    if h != _registered_hash:
        _post("custom", {"customEventsHash": h, "eventTypes": reg["eventTypes"]}, tok)
        _registered_hash = h
    return h


def push(entity, events, reg, tok, session=None, batch=1):
    h = ensure_registered(reg, tok)
    return _post("in", {"dataSessionId": session or str(uuid.uuid4()), "batch": batch,
                        "customEventsHash": h, "installId": entity, "events": events}, tok)


def _run():
    global _last_error
    while True:
        item = _q.get()
        try:
            tok = token()
            if tok:
                entity, evs, reg = item
                push(entity, evs, reg, tok)
                _last_error = None
        except Exception as e:  # noqa: BLE001 - best effort
            _last_error = "%s: %s" % (type(e).__name__, e)
            print("[keewano] push failed: %s" % _last_error, flush=True)


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="keewano", daemon=True)
            _worker.start()


# ---------------------------------------------------------------- public API

def emit(name, props=None):
    """Log an event. Never raises; the network part never blocks the caller."""
    try:
        props = props or {}
        ts = time.time()
        rec = {"ts": round(ts, 3), "name": name, "props": props}
        os.makedirs(DATA, exist_ok=True)
        with _file_lock:
            with open(EVENTS_PATH, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        if token():
            entity, evs, reg = build_events(name, json.loads(json.dumps(props, default=str)), int(ts))
            _ensure_worker()
            _q.put_nowait((entity, evs, reg))
    except Exception as e:  # noqa: BLE001
        try:
            print("[keewano] emit error: %s: %s" % (type(e).__name__, e), flush=True)
        except Exception:  # noqa: BLE001
            pass


def status():
    return {"token_set": bool(token()), "queued": _q.qsize(), "last_error": _last_error}


def flush(timeout=5.0):
    """Wait (bounded) for queued events to be sent; handy at process exit."""
    t0 = time.time()
    while not _q.empty() and time.time() - t0 < timeout:
        time.sleep(0.05)
    time.sleep(0.2)


if __name__ == "__main__":
    # offline self-check in a temp dir (does not touch data/): file append + event building, no network
    import tempfile
    d = tempfile.mkdtemp(prefix="kw_selfcheck_")
    DATA, EVENTS_PATH, REGISTRY_PATH = d, d + "/events.jsonl", d + "/reg.json"
    os.environ.pop("KEEWANO_TOKEN", None)
    emit("selfcheck", {"person": "Test Person", "accepted": True, "minutes_shifted": 30})
    assert sum(1 for _ in open(EVENTS_PATH)) == 1
    ent, evs, reg = build_events("negotiation_outcome", {"person": "A", "accepted": False, "saved_min": 12.4, "x": None}, 1)
    assert evs[-1]["dataType"] == "none" and len(evs) == 4, evs
    ent2, evs2, reg2 = build_events("negotiation_outcome", {"person": "A", "accepted": False, "saved_min": 12.4}, 2)
    assert [e["eventTypeId"] for e in evs2] == [e["eventTypeId"] for e in evs], "ids must be stable"
    assert ent == ent2 and to_unix("2027-10-12T09:30:00") > 0
    print("keewano.py self-check OK; registered types:", [t["name"] for t in reg2["eventTypes"]])
