"""HomeBy4 server (C3). Stdlib only. `python3 server.py` -> http://localhost:8787

Env: DIAL_API_KEY (unset => all calls mock; set => only meetings with "live": true in week.json are really called),
DIAL_NUMBER, LLM_BASE_URL/LLM_API_KEY/LLM_MODEL (Azure OpenAI extraction after live calls), KEEWANO_TOKEN, PORT (default 8787).
HOMEBY4_HOME (home address, geocoded once, cached in data/home.json), HOMEBY4_HOME_LABEL; see geo.py.
"""
import datetime
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import dial
import geo
import keewano

try:  # written by another agent; without it (or without HOMEBY4_CALENDAR_SYNC=1) calendar status stays "off"
    import calendar_sync
except Exception as _e:  # noqa: BLE001
    calendar_sync = None
    if not isinstance(_e, ImportError):
        print("[server] calendar_sync import failed: %s: %s" % (type(_e).__name__, _e), flush=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
WEEK_PATH = os.environ.get("HOMEBY4_WEEK") or os.path.join(ROOT, "data", "week.json")
WINDOW = ("2027-10-11", "2027-10-15")
# Demo only: the one number Dial may call (our own phone; Olin answers it in the video).
# The real number is set in .env as HOMEBY4_DEMO_PHONE so it stays out of this public repo.
# data/week.json carries the fictional placeholder below; every meeting is routed to DEMO_PHONE.
PLACEHOLDER_PHONE = "+15555550100"
DEMO_PHONE = os.environ.get("HOMEBY4_DEMO_PHONE", PLACEHOLDER_PHONE)
STALE_CALL_SECONDS = 240  # a call that never terminates is marked failed after this

LOCK = threading.RLock()
CALLS = {}      # call id -> call dict (C3 shape + internal fields prefixed with "_")
CONFIRMED = {}  # meeting id -> C2 confirmed entry
CAL = {"status": "off", "error": None, "last_sync": None, "name": None}  # /api/state "calendar"
_CAL = {"pending": None, "running": False, "timer": None}  # single-flight background sync
_SOLVE_CACHE = {}


def load_week():
    with open(WEEK_PATH) as f:
        week = json.load(f)
    lo, hi = WINDOW
    for m in week["meetings"]:
        if not (lo <= m["date"] <= hi):
            raise SystemExit("refusing to start: meeting %s is dated %s, outside %s..%s" % (m["id"], m["date"], lo, hi))
        if "phone" in m:
            if m["phone"] not in (PLACEHOLDER_PHONE, DEMO_PHONE):
                raise SystemExit("refusing to start: meeting %s has phone %r, only %s is allowed" % (m["id"], m["phone"], PLACEHOLDER_PHONE))
            m["phone"] = DEMO_PHONE
    w = week.get("window", {})
    if (w.get("start"), w.get("end")) != WINDOW:
        raise SystemExit("refusing to start: week window %s is not %s..%s" % (w, lo, hi))
    if week.get("demo_phone", PLACEHOLDER_PHONE) not in (PLACEHOLDER_PHONE, DEMO_PHONE):
        raise SystemExit("refusing to start: demo_phone must be %s" % PLACEHOLDER_PHONE)
    week["demo_phone"] = DEMO_PHONE
    geo.localize(week)  # real home ($HOMEBY4_HOME) + OSRM drive-time matrix; both fall back cleanly when offline
    return week


WEEK = None         # data/week.json + geo.localize (real home, drive matrix): guards, /api/state "week", calendar
SOLVE_WEEK = None   # what the solver sees: WEEK, or triage.effective_week(WEEK, TRIAGE) (not-worth-calling meetings are fixed)
TRIAGE = {}         # meeting id -> {worth_calling, movable, reason, source}
MEETINGS = {}


# ---------------------------------------------------------------- solving

def solve_day(date):
    """solver.solve with a cache keyed on the confirmed entries that matter for that date."""
    import solver
    with LOCK:
        ids = [m["id"] for m in WEEK["meetings"] if m["date"] == date]
        conf = {i: CONFIRMED[i] for i in ids if i in CONFIRMED}
    key = (date, json.dumps(conf, sort_keys=True))
    hit = _SOLVE_CACHE.get(key)
    if hit is None:
        hit = annotate(solver.solve(SOLVE_WEEK, date, conf), conf)
        h = SOLVE_WEEK["places"][SOLVE_WEEK["home"]]
        hit["sunset"] = geo.sunset(h["lat"], h["lng"], date, SOLVE_WEEK.get("tz") or "America/Los_Angeles")   # at home, local "HH:MM"
        if len(_SOLVE_CACHE) > 200:
            _SOLVE_CACHE.clear()
        _SOLVE_CACHE[key] = hit
    return hit


def public_week():
    """WEEK for /api/state minus the home coordinates and the drive matrix: the page needs neither and the server listens on 0.0.0.0."""
    w = {k: v for k, v in WEEK.items() if k != "drive_min"}
    w["places"] = dict(WEEK["places"], **{WEEK["home"]: {"name": WEEK["places"][WEEK["home"]]["name"]}})
    return w


def stage_of(entry):
    """proposed (no entry) | asked (windows/places only) | confirmed (fixed_start)."""
    if not entry:
        return "proposed"
    return "confirmed" if "fixed_start" in entry else "asked"


def annotate(res, conf):
    """solve() lists only meetings that moved vs BEFORE. Add stage + chosen_start/chosen_place to every move, and also
    list meetings that were asked/confirmed but stayed put. potential = some real move is not yet confirmed."""
    out = dict(res)
    after = {e["id"]: e for e in res["after"]["events"]}
    moves, seen = [], set()
    for mv in res["moves"]:
        e = after.get(mv["id"], {})
        moves.append(dict(mv, stage=stage_of(conf.get(mv["id"])), chosen_start=e.get("start", mv["to_start"]),
                          chosen_place=e.get("place", mv["to_place"])))
        seen.add(mv["id"])
    for mid in conf:
        m = MEETINGS.get(mid)
        if m and m["date"] == res["date"] and mid not in seen:
            e = after.get(mid, {})
            pl = None if (m.get("kind") == "remote" or not m.get("place")) else m["place"]
            moves.append({"id": mid, "person": m.get("person"), "from_start": m["start"], "to_start": e.get("start", m["start"]),
                          "from_place": pl, "to_place": e.get("place", pl), "stage": stage_of(conf[mid]),
                          "chosen_start": e.get("start", m["start"]), "chosen_place": e.get("place", pl)})
    moves.sort(key=lambda mv: (mv["to_start"], mv["id"]))
    out["moves"] = moves
    real = {x["id"] for x in res["moves"]}
    out["potential"] = any(mv["stage"] != "confirmed" for mv in moves if mv["id"] in real)
    return out


def solve_all():
    days = {}
    d = datetime.date.fromisoformat(WINDOW[0])
    end = datetime.date.fromisoformat(WINDOW[1])
    while d <= end:
        ds = d.isoformat()
        try:
            days[ds] = solve_day(ds)
        except Exception as e:  # noqa: BLE001 - keep the page alive if one day fails
            days[ds] = {"date": ds, "error": "%s: %s" % (type(e).__name__, e)}
        d += datetime.timedelta(days=1)
    return days


# ---------------------------------------------------------------- calls

def public_call(c):
    out = {k: v for k, v in c.items() if not k.startswith("_")}
    out["call"] = {k: v for k, v in out.items()}  # tolerate clients that expect {call: {...}} or the bare call
    return out


def live_meetings():
    """Meeting ids whose calls are real phone calls (DIAL_API_KEY set AND meeting has "live": true)."""
    return [m["id"] for m in WEEK["meetings"] if dial.is_live(m)]


def hhmm_add(hhmm, delta):
    h, m = hhmm.split(":")
    t = int(h) * 60 + int(m) + delta
    return "%02d:%02d" % (t // 60, t % 60)


def weekday(date):
    return datetime.date.fromisoformat(date).strftime("%A")


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def realized(date, conf):
    """Metrics for the day if ONLY confirmed (accepted/pinned) moves are applied; everything else stays put.
    -> dict(home_before, dead_before, home_after, saved_min, moves)"""
    import solver
    base, real, moves = [], [], 0
    for m in WEEK["meetings"]:
        if m["date"] != date:
            continue
        base.append({"id": m["id"], "start": m["start"], "place": m.get("place")})
        c = conf.get(m["id"]) or {}
        if "fixed_start" in c:
            pl = None if (m.get("kind") == "remote" or not m.get("place")) else (c.get("places") or [m["place"]])[0]
            real.append({"id": m["id"], "start": c["fixed_start"], "place": pl})
            moves += (c["fixed_start"] != m["start"]) or (pl != m.get("place"))
        else:
            real.append(base[-1])
    b, r = solver.metrics(SOLVE_WEEK, date, base), solver.metrics(SOLVE_WEEK, date, real)
    return {"home_before": b["home_at"], "dead_before": b["dead_min"], "home_after": r["home_at"],
            "saved_min": max(0, _hm(b["home_at"]) - _hm(r["home_at"])), "moves": int(moves)}


def apply_outcome(call, res):
    """res = dial.extract_ask / extract_inform result. Update CONFIRMED and emit Keewano events (names/props match
    data/keewano_seed.jsonl plus call_kind). Called once per call, with LOCK held."""
    m = MEETINGS[call["meeting_id"]]
    remote = m.get("kind") == "remote" or not m.get("place")
    own = [] if remote else [m["place"]]
    try:
        saved_before = realized(m["date"], CONFIRMED)["saved_min"]
    except Exception:  # noqa: BLE001
        saved_before = 0
    if call["kind"] == "ask":
        windows = res["windows"]
        if windows:
            entry = {"windows": windows, "places": ([] if remote else (res["places_ok"] or own))}
            kind = "windows"
        else:
            entry = {"fixed_start": m["start"], "places": own}  # declined / nothing usable: pin the original
            kind = "declined"
        outcome = {"accepted": False, "start": None, "place": None, "windows": windows,
                   "places_ok": res["places_ok"], "declined": kind == "declined", "via": res.get("via")}
    else:
        agreed = res["agreed"]
        if agreed:
            entry = {"fixed_start": call["to_start"], "places": [] if remote else [p for p in [call["to_place"]] if p]}
        else:
            entry = CONFIRMED.get(m["id"])  # leave windows as they were
        kind = "agreed" if agreed else "not_agreed"
        outcome = {"accepted": agreed, "agreed": agreed, "start": call["to_start"], "place": call["to_place"],
                   "windows": [], "via": res.get("via")}
    if entry:
        CONFIRMED[m["id"]] = entry
        calendar_changed()
    call["outcome"], call["result"], call["confirmed"] = outcome, kind, entry
    try:
        rz = realized(m["date"], CONFIRMED)
    except Exception as e:  # noqa: BLE001
        print("[server] realized() failed: %s" % e, flush=True)
        rz = None
    mm = re.search(r"\(([^)]+)\)\s*$", m.get("title", ""))
    prop = call["to_start"] or m["start"]
    keewano.emit("call_outcome", {
        "person": m["person"], "role": mm.group(1) if mm else "unknown", "meeting_title": m.get("title", ""),
        "weekday": weekday(m["date"]), "proposed_start": prop, "proposed_shift_min": _hm(prop) - _hm(m["start"]),
        "accepted": kind == "agreed", "countered": kind == "windows",
        "minutes_saved": max(0, rz["saved_min"] - saved_before) if rz else 0,
        "call_seconds": round(time.time() - call["_t0"]), "meeting_id": m["id"], "mode": call["mode"], "call_kind": call["kind"]})
    if rz and call["kind"] == "inform":
        keewano.emit("day_compacted", {
            "date": m["date"], "weekday": weekday(m["date"]), "saved_min": rz["saved_min"], "moves": rz["moves"],
            "home_before": rz["home_before"], "home_after": rz["home_after"], "dead_min_before": rz["dead_before"]})


def _parse_worker(call, turns):
    """Runs outside LOCK (the LLM extractor may take up to 45 s), then applies the outcome under LOCK."""
    m = MEETINGS[call["meeting_id"]]
    t0 = time.time()
    try:
        if call["kind"] == "ask":
            res = dial.extract_ask(turns, m, WEEK, use_llm=call["mode"] == "live")
        else:
            res = dial.extract_inform(turns, m, call["to_start"], use_llm=call["mode"] == "live")
    except Exception as e:  # noqa: BLE001
        print("[server] extraction failed: %s: %s" % (type(e).__name__, e), flush=True)
        res = None
    with LOCK:
        if CALLS.get(call["id"]) is call:  # a reset in the meantime drops the result
            try:
                if res is None:
                    call["outcome"], call["result"] = None, "unparsed"
                    keewano.emit("call_failed", {"meeting_id": call["meeting_id"], "person": call["person"], "reason": "unparsed"})
                else:
                    call["parse_seconds"] = round(time.time() - t0, 1)
                    apply_outcome(call, res)
            except Exception as e:  # noqa: BLE001
                print("[server] apply_outcome failed: %s: %s" % (type(e).__name__, e), flush=True)
                call["outcome"], call["result"] = None, "unparsed"
            call["status"] = "ended"
        _SOLVE_CACHE.clear()


def refresh_call(call):
    """Pull latest state from Dial/mock; on termination parse the outcome once. LOCK held by caller."""
    if call["status"] in ("ended", "failed", "parsing"):
        return
    try:
        if time.time() - call["_t0"] > STALE_CALL_SECONDS:
            call["status"], call["error"] = "failed", "call timed out"
            return
        r = dial.get_call(call["_dial_id"])
    except Exception as e:  # noqa: BLE001 - transient fetch problem: keep last state, UI keeps polling
        call["warning"] = str(e)
        return
    call.pop("warning", None)
    call["turns"] = r["turns"]
    call["status"] = r["status"]
    if r["status"] == "in_progress" and any(t.get("speaker") == "agent" and "goodbye" in t.get("text", "").lower()
                                            for t in r["turns"]):
        threading.Timer(2.5, dial.stop_call, args=(call["_dial_id"],)).start()  # let "Goodbye" finish playing
    if r["status"] == "failed":
        call["error"] = r.get("reason", "failed")
        keewano.emit("call_failed", {"meeting_id": call["meeting_id"], "person": call["person"], "reason": str(call["error"])})
    elif r["status"] == "ended":
        call["status"] = "parsing"  # extraction runs in a background thread; polls see "parsing" until it is done
        threading.Thread(target=_parse_worker, args=(call, r["turns"]), daemon=True).start()


def create_call(body):
    mid, kind = body.get("meeting_id"), body.get("kind") or "ask"
    m = MEETINGS.get(mid)  # to_start / to_place in the body are ignored
    if kind not in ("ask", "inform"):
        raise HttpError(400, "kind must be 'ask' or 'inform'")
    if not m:
        raise HttpError(404, "unknown meeting_id %r" % (mid,))
    if not m.get("flexible"):
        raise HttpError(400, "meeting %s is not flexible" % mid)
    to_start = to_place = None
    if kind == "inform":  # the solver's current choice for this meeting, frozen at call time
        ev = next((e for e in solve_day(m["date"])["after"]["events"] if e["id"] == mid), None)
        if not ev:
            raise HttpError(400, "meeting %s is not in the day plan" % mid)
        to_start, to_place = ev["start"], ev["place"]
    with LOCK:
        for c in CALLS.values():
            refresh_call(c)
            if c["status"] in ("queued", "in_progress"):
                raise HttpError(409, "another call is still in progress (%s); one call at a time" % c["id"])
        place_name = WEEK["places"][to_place]["name"] if to_place else ""
        try:
            dial_id = dial.start_call(m, to_start or m["start"], place_name, SOLVE_WEEK, dict(CONFIRMED), kind=kind)  # single attempt, never retried
        except ValueError as e:
            raise HttpError(403, str(e))
        except RuntimeError as e:
            keewano.emit("call_failed", {"meeting_id": mid, "person": m["person"], "reason": "start_failed"})
            raise HttpError(502, str(e))
        call = {"id": dial_id, "meeting_id": mid, "person": m["person"], "kind": kind, "status": "queued", "turns": [],
                "outcome": None, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "to_start": to_start, "to_place": to_place, "from_start": m["start"], "from_place": m["place"],
                "mode": dial.call_mode(m), "_dial_id": dial_id, "_t0": time.time()}
        CALLS[dial_id] = call
        keewano.emit("call_placed", {"meeting_id": mid, "person": m["person"], "date": m["date"],
                                     "weekday": weekday(m["date"]), "from_start": m["start"],
                                     "proposed_start": to_start or m["start"], "mode": call["mode"], "call_kind": kind})
        return public_call(call)


# ---------------------------------------------------------------- simulated calls (everyone except the live meeting)

def _sim_call(m, kind):
    """One instant simulated call (mock transcript, regex extraction, no network/LLM); applies the outcome like a real call.
    LOCK held by the caller."""
    to_start = to_place = None
    if kind == "inform":  # the solver's current choice, frozen now
        ev = next((e for e in solve_day(m["date"])["after"]["events"] if e["id"] == m["id"]), None)
        to_start, to_place = (ev["start"], ev["place"]) if ev else (m["start"], m.get("place"))
    turns, secs = dial.simulate_call(m, SOLVE_WEEK, kind, dict(CONFIRMED), to_start,
                                     WEEK["places"][to_place]["name"] if to_place else "")
    cid = "sim-" + uuid.uuid4().hex[:8]
    call = {"id": cid, "meeting_id": m["id"], "person": m["person"], "kind": kind, "status": "ended", "turns": turns,
            "outcome": None, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "to_start": to_start, "to_place": to_place, "from_start": m["start"], "from_place": m["place"],
            "mode": "mock", "simulated": True, "parse_seconds": 0, "_dial_id": cid, "_t0": time.time() - secs}
    CALLS[cid] = call
    keewano.emit("call_placed", {"meeting_id": m["id"], "person": m["person"], "date": m["date"], "weekday": weekday(m["date"]),
                                 "from_start": m["start"], "proposed_start": to_start or m["start"], "mode": "mock", "call_kind": kind})
    res = (dial.extract_ask(turns, m, WEEK, use_llm=False) if kind == "ask"
           else dial.extract_inform(turns, m, to_start, use_llm=False))
    if res is None:
        call["result"] = "unparsed"
        keewano.emit("call_failed", {"meeting_id": m["id"], "person": m["person"], "reason": "unparsed"})
    else:
        apply_outcome(call, res)
    return call


def simulate_week():
    """Act as if every NON-live meeting with a proposed move had been called: simulated ask (-> windows), then simulated
    inform (-> fixed_start of the solver's choice). Up to 3 passes, until no non-live move is left without a CONFIRMED entry.
    -> list of the new call dicts."""
    created = []
    with LOCK:
        for _ in range(3):
            todo = [MEETINGS[mv["id"]] for d in solve_all().values() for mv in d.get("moves", [])
                    if mv["stage"] == "proposed" and mv["id"] not in CONFIRMED
                    and not MEETINGS[mv["id"]].get("live") and MEETINGS[mv["id"]].get("flexible")]
            if not todo:
                break
            created += [_sim_call(m, "ask") for m in todo]
            created += [_sim_call(m, "inform") for m in todo if "fixed_start" not in (CONFIRMED.get(m["id"]) or {})
                        and m["id"] in CONFIRMED]  # declined asks are already pinned
        _SOLVE_CACHE.clear()
    return created


# ---------------------------------------------------------------- calendar sync (opt-in: HOMEBY4_CALENDAR_SYNC=1)

def calendar_enabled():
    return calendar_sync is not None and os.environ.get("HOMEBY4_CALENDAR_SYNC") == "1"


def build_sched():
    """{meeting_id: {start, place}} for ALL meetings: confirmed fixed_start/places, else the original. LOCK held."""
    sched = {}
    for m in WEEK["meetings"]:
        c = CONFIRMED.get(m["id"]) or {}
        remote = m.get("kind") == "remote" or not m.get("place")
        if "fixed_start" in c:
            sched[m["id"]] = {"start": c["fixed_start"], "place": None if remote else (c.get("places") or [m["place"]])[0]}
        else:
            sched[m["id"]] = {"start": m["start"], "place": None if remote else m["place"]}
    return sched


def _cal_kick(job, delay=0.0):
    """Queue a calendar job ("push" | "reset"; reset wins) for the single-flight background worker. No-op when sync is off."""
    if not calendar_enabled():
        return
    with LOCK:
        if job == "reset" or _CAL["pending"] != "reset":
            _CAL["pending"] = job
        if _CAL["timer"]:
            _CAL["timer"].cancel()
        t = _CAL["timer"] = threading.Timer(delay, _cal_worker)
        t.daemon = True
        t.start()
        CAL["status"] = "syncing"


def calendar_changed():
    _cal_kick("push", 1.0)  # debounce: a burst of confirmations becomes one push


def _cal_worker():
    with LOCK:
        if _CAL["running"]:
            return  # the running worker picks up _CAL["pending"] when it finishes
        _CAL["running"] = True
    while True:
        with LOCK:
            job, _CAL["pending"] = _CAL["pending"], None
            if job is None:
                _CAL["running"] = False
                return
            CAL["status"] = "syncing"
            sched = build_sched()
        err = None
        try:
            if job == "reset":
                calendar_sync.reset(WEEK)
            else:
                calendar_sync.push(WEEK, sched)
        except Exception as e:  # noqa: BLE001 - osascript failures etc.; surfaced in /api/state calendar.error
            err = "%s: %s" % (type(e).__name__, e)
            print("[server] calendar %s failed: %s" % (job, err), flush=True)
        with LOCK:
            if err:
                CAL["status"], CAL["error"] = "error", err
            else:
                CAL["status"] = "idle" if job == "reset" else "synced"
                CAL["error"] = None
                CAL["last_sync"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            if _CAL["pending"]:
                CAL["status"] = "syncing"


def _cal_probe():
    try:
        r = calendar_sync.status(WEEK)
        with LOCK:
            CAL["name"] = (r.get("name") or r.get("calendar")) if isinstance(r, dict) else (str(r) if r else None)
    except Exception as e:  # noqa: BLE001
        with LOCK:
            CAL["status"], CAL["error"] = "error", "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- HTTP

class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeBy4/1.0"
    protocol_version = "HTTP/1.1"

    def log_request(self, code="-", size="-"):  # one line per request
        print("%s %s %s %s %.0fms" % (time.strftime("%H:%M:%S"), self.command, self.path, code,
                                      (time.time() - getattr(self, "_t", time.time())) * 1000), flush=True)

    def log_message(self, fmt, *args):  # errors from the http.server machinery
        print("%s [http] %s" % (time.strftime("%H:%M:%S"), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise HttpError(413, "body too large")
        raw = self.rfile.read(n) if n else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "body is not valid JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        return data

    def _static(self, rel):
        full = os.path.realpath(os.path.join(WEB, rel))
        if not (full == WEB or full.startswith(WEB + os.sep)) or not os.path.isfile(full):
            raise HttpError(404, "not found: /web/%s" % rel if rel != "index.html" else "web/index.html not found yet")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _dispatch(self):
        self._t = time.time()
        path = urlparse(self.path).path
        try:
            if self.command in ("GET", "HEAD"):
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/web/"):
                    return self._static(unquote(path[5:]))
                if path == "/api/ics":  # current week incl. confirmed moves; open it to update the calendar
                    sys.path.insert(0, os.path.join(ROOT, "data"))
                    import make_ics
                    with LOCK:
                        body = make_ics.build(WEEK, dict(CONFIRMED)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/calendar; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="homeby4_week.ics"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if path == "/favicon.ico":
                    return self._send(204, b"")
                if path == "/api/state":
                    with LOCK:
                        for c in CALLS.values():
                            refresh_call(c)
                        calls = [public_call(c)["call"] for c in CALLS.values()]
                        confirmed = dict(CONFIRMED)
                        calendar = dict(CAL)
                    return self._send(200, {"week": public_week(), "days": solve_all(), "calls": calls, "calendar": calendar, "triage": TRIAGE,
                                            "mode": dial.mode(), "live_meetings": live_meetings(),
                                            "confirmed": confirmed,
                                            "keewano": keewano.status()})
                m = re.fullmatch(r"/api/call/([\w.\-]+)", path)
                if m:
                    with LOCK:
                        c = CALLS.get(m.group(1))
                        if not c:
                            raise HttpError(404, "unknown call %s" % m.group(1))
                        refresh_call(c)
                        out = public_call(c)
                    if out["status"] == "ended" and out.get("outcome"):
                        out["day"] = solve_day(MEETINGS[out["meeting_id"]]["date"])
                    return self._send(200, out)
                raise HttpError(404, "no such endpoint %s" % path)
            if self.command == "POST":
                body = self._body()  # always drain the body, or keep-alive reads it as the next request (501)
                if path == "/api/call":
                    return self._send(200, create_call(body))
                if path == "/api/simulate":
                    calls = [public_call(c)["call"] for c in simulate_week()]
                    return self._send(200, {"calls": calls, "days": solve_all()})
                if path == "/api/calendar/reset":
                    if not calendar_enabled():
                        raise HttpError(400, "calendar sync is off (needs HOMEBY4_CALENDAR_SYNC=1 and calendar_sync.py)")
                    _cal_kick("reset")
                    with LOCK:
                        return self._send(200, {"ok": True, "calendar": dict(CAL)})
                if path == "/api/reset":
                    with LOCK:
                        CALLS.clear()
                        CONFIRMED.clear()
                    _cal_kick("reset")
                    return self._send(200, {"ok": True})
                raise HttpError(404, "no such endpoint %s" % path)
            raise HttpError(405, "method not allowed")
        except HttpError as e:
            self._send(e.code, {"error": e.msg})
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            try:
                self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})
            except Exception:  # noqa: BLE001
                pass

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _dispatch


def load_triage():
    """Optional triage.py (written by another agent): LLM verdicts on which meetings are worth calling. Any problem => raw week."""
    global SOLVE_WEEK, TRIAGE
    SOLVE_WEEK, TRIAGE = WEEK, {}
    try:
        import triage
    except ImportError:
        return
    try:
        verdicts = triage.load_or_run(WEEK)
        if isinstance(verdicts, list):
            verdicts = {v["id"]: v for v in verdicts if isinstance(v, dict) and "id" in v}
        TRIAGE = verdicts or {}
        SOLVE_WEEK = triage.effective_week(WEEK, TRIAGE)
    except Exception as e:  # noqa: BLE001
        SOLVE_WEEK = WEEK
        print("[server] triage failed (%s: %s); solving the raw week" % (type(e).__name__, e), flush=True)


def main():
    global WEEK, MEETINGS
    WEEK = load_week()
    MEETINGS = {m["id"]: m for m in WEEK["meetings"]}
    load_triage()
    port = int(os.environ.get("PORT", "8787"))
    if calendar_enabled():
        CAL["status"] = "idle"
        threading.Thread(target=_cal_probe, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("HomeBy4 on http://localhost:%d  mode=%s  live_meetings=%s  llm=%s  calendar=%s  keewano=%s  meetings=%d"
          % (port, dial.mode(), live_meetings(), "azure" if dial.llm_configured() else "regex-only", CAL["status"],
             "on" if keewano.token() else "log-only", len(MEETINGS)), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        keewano.flush()


if __name__ == "__main__":
    main()
