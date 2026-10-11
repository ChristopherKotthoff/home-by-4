"""Push data/keewano_seed.jsonl (8 weeks of synthetic negotiation history) to Keewano. Stdlib only.

Usage: python3 backfill.py [--dry-run] [--no-shift] [path/to/seed.jsonl]
With KEEWANO_TOKEN set (and no --dry-run) it registers the custom event types, then posts each entity's events
in time order. Without a token it prints exactly what it would send. One request per entity chunk, no retries
(a retried batch could duplicate events). Seed lines are {ts, name, props} like data/events.jsonl; ts may be
unix seconds or an ISO string; "event"/"timestamp"/"properties" are accepted as aliases.
"""
import collections
import json
import os
import sys
import time
import uuid

import keewano

WEEK_S = 7 * 86400
CHUNK = 150  # Keewano events per request


def load(path):
    rows, bad = [], 0
    with open(path) as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                name = r.get("name") or r.get("event")
                props = r.get("props") if r.get("props") is not None else r.get("properties")
                if props is None:
                    props = {k: v for k, v in r.items() if k not in ("ts", "timestamp", "name", "event")}
                rows.append((keewano.to_unix(r.get("ts", r.get("timestamp"))), str(name), props))
            except (ValueError, AttributeError, TypeError):
                bad += 1
                print("skipping bad line %d" % ln)
    return rows, bad


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if not a.startswith("--")]
    path = args[0] if args else os.path.join(keewano.DATA, "keewano_seed.jsonl")
    if not os.path.exists(path):
        print("seed file not found: %s (WP1 creates it)" % path)
        return 1
    rows, bad = load(path)
    # The seed is dated before the demo week (Aug-Oct 2027) which is still in the future today; Keewano may reject or
    # hide future-dated events, so shift by whole weeks (weekday preserved) until the newest event is in the past.
    shift_weeks = 0
    if "--no-shift" not in argv and rows:
        over = max(r[0] for r in rows) - int(time.time())
        if over > 0:
            shift_weeks = -(-over // WEEK_S)
            rows = [(ts - shift_weeks * WEEK_S, n, p) for ts, n, p in rows]
            print("shifted all seed timestamps back %d weeks so they are not in the future (use --no-shift to disable)" % shift_weeks)
    rows.sort(key=lambda r: r[0])
    by_entity = collections.OrderedDict()
    reg = None
    for ts, name, props in rows:
        ent, evs, reg = keewano.build_events(name, props, ts)
        by_entity.setdefault(ent, []).extend(evs)
    tok = keewano.token()
    live = bool(tok) and not dry
    n_events = sum(len(v) for v in by_entity.values())
    print("seed: %d lines (%d unreadable) -> %d Keewano events across %d entities; %d custom event types"
          % (len(rows), bad, n_events, len(by_entity), len(reg["eventTypes"]) if reg else 0))
    if reg:
        print("custom types:", ", ".join("%d=%s(%s)" % (t["eventTypeId"], t["name"], t["dataType"]) for t in reg["eventTypes"]))
    if not live:
        print("DRY RUN (%s). Would POST %s/custom once, then %s/in per entity chunk with header k-token."
              % ("no KEEWANO_TOKEN" if not tok else "--dry-run", keewano.API, keewano.API))
        if by_entity:
            ent, evs = next(iter(by_entity.items()))
            print("first request body would be:")
            print(json.dumps({"dataSessionId": "<uuid4>", "batch": 1, "customEventsHash": keewano.registry_hash(reg),
                              "installId": ent, "events": evs[:4]}, indent=1) + "\n  ... (%d events)" % len(evs))
        return 0
    sent = failed = 0
    keewano.ensure_registered(reg, tok)
    print("registered custom events, hash", keewano.registry_hash(reg))
    for ent, evs in by_entity.items():
        session = str(uuid.uuid4())
        for i in range(0, len(evs), CHUNK):
            try:
                keewano.push(ent, evs[i:i + CHUNK], reg, tok, session=session, batch=i // CHUNK + 1)
                sent += len(evs[i:i + CHUNK])
            except Exception as e:  # noqa: BLE001
                failed += len(evs[i:i + CHUNK])
                print("  FAILED entity %s chunk %d: %s" % (ent, i // CHUNK + 1, e))
        print("  entity %s: %d events" % (ent, len(evs)))
    print("done: sent=%d failed=%d (events become queryable ~5 min after ingest)" % (sent, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
