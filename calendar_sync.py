#!/usr/bin/env python3
"""calendar_sync.py - write the HomeBy4 demo week to the user's real macOS Calendar.

Stdlib + `osascript` (AppleScript) only.  API (imported by server.py):

    status(week)       -> {meeting_id: {"start","end","date","title","location","found"}}
    push(week, sched)  -> {"updated": n, "missing": [ids], "changes": [one line per event]}
                          sched = {meeting_id: {"start": "HH:MM", "place": place_id|None}}
    reset(week)        -> same shape as push; puts the whole week back to week.json
    restore_snapshot(week) -> same shape; exact state saved before our first write

push sets title (week.json), start, end (= start + dur) and location
(week["places"][place]["name"], or "Phone / video call" for remote meetings;
place None on an in-person meeting keeps its original place).  Only events
that actually differ are written, so `updated` counts real changes.

RESET RULE (chosen): reset() restores times and locations from week.json
originals and titles from week.json (== push of the originals; the CLI calls
that `push-original`).  The week.json originals are the demo's "before" state, so
the tue-5 title ends up "Coffee: Olin (friend)", not the old "Dana Whitfield"
title.  Before the first write ever (push / reset / push-original), the current
state of every matched event is saved to data/calendar_snapshot.json (kept out
of git); restore_snapshot() / CLI `restore-snapshot` puts back exactly that,
legacy titles included.

MATCHING (Calendar.app did NOT keep the ICS UIDs; it assigned its own):
  1. uid "<meeting_id>@homeby4.demo" (if a Calendar ever keeps ICS UIDs),
  2. the Calendar uid stored in the snapshot (stable across moves and renames),
  3. (date, title) where title is the week.json title or a known legacy title
     (tue-5: "Coffee: Dana Whitfield (angel)"); closest original start wins.
Extra duplicates of an event are never touched.

SAFETY (hard): only the calendar named by env HOMEBY4_CALENDAR (default
"Privat"); only events whose start date is within 2027-10-11..2027-10-15 (in
the week's time zone); only events matched to a week.json meeting id; nothing
is ever deleted; a week.json meeting outside the window raises ValueError.
osascript runs under subprocess timeout=60; failures raise RuntimeError.
Data goes to AppleScript via a temp TSV file / argv, never into script code.

CLI: python3 calendar_sync.py status | reset | push-original | restore-snapshot
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
WEEK_FILE = Path(os.environ.get("HOMEBY4_WEEK") or HERE / "data" / "week.json")
SNAPSHOT_FILE = HERE / "data" / "calendar_snapshot.json"
WIN_START, WIN_END = date(2027, 10, 11), date(2027, 10, 15)  # inclusive, hard limit
REMOTE_LOC = "Phone / video call"
UID_SUFFIX = "@homeby4.demo"
LEGACY_TITLES = {"tue-5": ("Coffee: Dana Whitfield (angel)",)}
_lock = threading.Lock()

# ---------------------------------------------------------------- AppleScript

_HANDLERS = r'''
on mkdate(str)
	set AppleScript's text item delimiters to ","
	set p to text items of str
	set AppleScript's text item delimiters to ""
	set d to current date
	set day of d to 1
	set year of d to (item 1 of p as integer)
	set month of d to (item 2 of p as integer)
	set day of d to (item 3 of p as integer)
	set time of d to ((item 4 of p as integer) * 3600 + (item 5 of p as integer) * 60)
	return d
end mkdate

on fmt(d)
	return ((year of d) as text) & "," & ((month of d as integer) as text) & "," & ((day of d) as text) & "," & ((hours of d) as text) & "," & ((minutes of d) as text)
end fmt

on repl(t, a, b)
	set AppleScript's text item delimiters to a
	set p to text items of t
	set AppleScript's text item delimiters to b
	set t to p as text
	set AppleScript's text item delimiters to ""
	return t
end repl

on clean(t)
	if t is missing value then return ""
	set t to my repl(t as text, tab, " ")
	set t to my repl(t, return, " ")
	return my repl(t, linefeed, " ")
end clean
'''

READ_SCRIPT = _HANDLERS + r'''
on run argv
	set calName to item 1 of argv
	set s to my mkdate(item 2 of argv)
	set e to my mkdate(item 3 of argv)
	set outLines to {}
	tell application "Calendar"
		tell calendar calName
			set props to properties of (every event whose start date >= s and start date < e)
			repeat with r in props
				set end of outLines to (id of r) & tab & my clean(summary of r) & tab & my fmt(start date of r) & tab & my fmt(end date of r) & tab & my clean(location of r)
			end repeat
		end tell
	end tell
	set AppleScript's text item delimiters to linefeed
	set res to outLines as text
	set AppleScript's text item delimiters to ""
	return res
end run
'''

# rows file: uid TAB title TAB start TAB end TAB location   (dates as y,m,d,h,min)
WRITE_SCRIPT = _HANDLERS + r'''
on run argv
	set calName to item 1 of argv
	set s to my mkdate(item 2 of argv)
	set e to my mkdate(item 3 of argv)
	set txt to read (POSIX file (item 4 of argv)) as «class utf8»
	set rows to {}
	repeat with ln in (paragraphs of txt)
		if (length of ln) > 0 then
			set AppleScript's text item delimiters to tab
			set end of rows to (text items of (ln as text))
			set AppleScript's text item delimiters to ""
		end if
	end repeat
	set done to {}
	tell application "Calendar"
		tell calendar calName
			set evs to (every event whose start date >= s and start date < e)
			repeat with ev in evs
				set u to uid of ev
				repeat with r in rows
					if (item 1 of r) is u then
						set ns to my mkdate(item 3 of r)
						set ne to my mkdate(item 4 of r)
						set summary of ev to (item 2 of r)
						if ns >= (end date of ev) then
							set end date of ev to ne
							set start date of ev to ns
						else
							set start date of ev to ns
							set end date of ev to ne
						end if
						set location of ev to (item 5 of r)
						set end of done to u
						exit repeat
					end if
				end repeat
			end repeat
		end tell
	end tell
	set AppleScript's text item delimiters to linefeed
	set res to done as text
	set AppleScript's text item delimiters to ""
	return res
end run
'''


def _osascript(script, *args):
    with _lock:
        try:
            p = subprocess.run(["osascript", "-", *args], input=script, capture_output=True,
                               text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Calendar did not answer within 60 s (is Calendar.app blocked by a dialog?)")
        except OSError as e:
            raise RuntimeError(f"cannot run osascript: {e}")
    if p.returncode != 0:
        err = p.stderr.strip()
        if "-1743" in err or "not authorized" in err.lower() or "not allowed" in err.lower():
            raise RuntimeError("macOS has not allowed this process to control Calendar "
                               "(System Settings > Privacy & Security > Automation)")
        if "-1728" in err and "calendar" in err.lower():
            raise RuntimeError(f"calendar {_calendar()!r} not found in Calendar.app")
        raise RuntimeError("Calendar script failed: " + err[:300])
    return p.stdout.rstrip("\n")


# ---------------------------------------------------------------- helpers

def _calendar():
    return os.environ.get("HOMEBY4_CALENDAR", "Privat")


def load_week(path=None):
    with open(path or WEEK_FILE) as f:
        return json.load(f)


def _tz(week):
    return ZoneInfo(week.get("tz") or "America/Los_Angeles")


def _check(week):
    for m in week["meetings"]:
        d = date.fromisoformat(m["date"])
        if not WIN_START <= d <= WIN_END:
            raise ValueError(f"meeting {m['id']} is on {m['date']}, outside {WIN_START}..{WIN_END}")


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _fmt_min(n):
    return f"{n // 60:02d}:{n % 60:02d}"


def _to_local_parts(tz, d, minutes):
    """Wall-clock `minutes` after midnight on date d in the week's tz -> 'y,m,d,h,min' in the Mac's tz."""
    base = datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(minutes=minutes)
    l = base.astimezone()
    return f"{l.year},{l.month},{l.day},{l.hour},{l.minute}"


def _from_local_parts(tz, parts):
    y, mo, d, h, mi = (int(x) for x in parts.split(","))
    return datetime(y, mo, d, h, mi).astimezone(tz).replace(tzinfo=None)  # naive datetime, week-tz wall clock


def _read_events(week):
    tz = _tz(week)
    out = _osascript(READ_SCRIPT, _calendar(), _to_local_parts(tz, WIN_START, 0),
                     _to_local_parts(tz, WIN_END + timedelta(days=1), 0))
    events = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) != 5:
            continue
        s, e = _from_local_parts(tz, f[2]), _from_local_parts(tz, f[3])
        if not WIN_START <= s.date() <= WIN_END:  # belt and braces
            continue
        events.append({"uid": f[0], "title": f[1], "date": s.date().isoformat(),
                       "start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"), "location": f[4]})
    return events


def _load_snapshot():
    try:
        with open(SNAPSHOT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _match(week, events):
    """-> {meeting_id: event}"""
    by_uid = {e["uid"]: e for e in events}
    snap = (_load_snapshot() or {}).get("events", {})
    got, used = {}, set()
    for m in week["meetings"]:
        for uid in (m["id"] + UID_SUFFIX, snap.get(m["id"], {}).get("uid")):
            e = by_uid.get(uid)
            if e and uid not in used and e["date"] == m["date"]:
                got[m["id"]] = e
                used.add(uid)
                break
    for m in week["meetings"]:
        if m["id"] in got:
            continue
        titles = (m["title"],) + LEGACY_TITLES.get(m["id"], ())
        cands = [e for e in events if e["uid"] not in used and e["date"] == m["date"] and e["title"] in titles]
        cands.sort(key=lambda e: (e["title"] != m["title"], abs(_hm(e["start"]) - _hm(m["start"]))))
        if cands:
            got[m["id"]] = cands[0]
            used.add(cands[0]["uid"])
    return got


def _location(week, m, place):
    if m["kind"] == "remote":
        return REMOTE_LOC
    place = place or m["place"]
    if place not in week["places"]:
        raise ValueError(f"unknown place {place!r} for {m['id']}")
    return week["places"][place]["name"]


def _target(week, m, start, place):
    s = _hm(start)
    if not (0 <= s and s + m["dur"] < 24 * 60):
        raise ValueError(f"{m['id']}: {start} + {m['dur']} min does not fit in one day")
    return {"title": m["title"], "start": _fmt_min(s), "end": _fmt_min(s + m["dur"]),
            "location": _location(week, m, place)}


def _originals(week):
    return {m["id"]: _target(week, m, m["start"], m["place"]) for m in week["meetings"]}


def _line(mid, cur, tgt):
    parts = []
    if cur["title"] != tgt["title"]:
        parts.append(f"title {cur['title']!r} -> {tgt['title']!r}")
    if (cur["start"], cur["end"]) != (tgt["start"], tgt["end"]):
        parts.append(f"{cur['start']}-{cur['end']} -> {tgt['start']}-{tgt['end']}")
    if cur["location"] != tgt["location"]:
        parts.append(f"at {cur['location']} -> {tgt['location']}")
    return f"{mid} ({cur['date']}): " + "; ".join(parts)


def _apply(week, targets):
    """targets: {meeting_id: {"title","start","end","location"}} -> {"updated","missing","changes"}"""
    _check(week)
    known = {m["id"]: m for m in week["meetings"]}
    for mid in targets:
        if mid not in known:
            raise ValueError(f"unknown meeting id {mid!r}")
    got = _match(week, _read_events(week))
    if got and _load_snapshot() is None:  # first write ever: remember the exact prior state
        SNAPSHOT_FILE.parent.mkdir(exist_ok=True)
        tmp = SNAPSHOT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"calendar": _calendar(), "saved_at": datetime.now().isoformat(timespec="seconds"),
                                   "events": got}, indent=1, ensure_ascii=False))
        tmp.replace(SNAPSHOT_FILE)
    tz, rows, changes = _tz(week), [], []
    missing = [mid for mid in targets if mid not in got]
    for mid, tgt in targets.items():
        cur = got.get(mid)
        if not cur or all(cur[k] == tgt[k] for k in ("title", "start", "end", "location")):
            continue
        d = date.fromisoformat(known[mid]["date"])
        clean = lambda t: " ".join(t.split())  # no tabs/newlines in the TSV
        rows.append("\t".join([cur["uid"], clean(tgt["title"]), _to_local_parts(tz, d, _hm(tgt["start"])),
                               _to_local_parts(tz, d, _hm(tgt["end"])), clean(tgt["location"])]))
        changes.append(_line(mid, cur, tgt))
    done = []
    if rows:
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", encoding="utf-8", delete=False) as f:
            f.write("\n".join(rows) + "\n")
        try:
            first = _to_local_parts(tz, WIN_START, 0)
            end = _to_local_parts(tz, WIN_END + timedelta(days=1), 0)
            done = [u for u in _osascript(WRITE_SCRIPT, _calendar(), first, end, f.name).split("\n") if u]
        finally:
            os.unlink(f.name)
    if len(done) != len(rows):
        raise RuntimeError(f"Calendar updated {len(done)} of {len(rows)} events")
    return {"updated": len(rows), "missing": missing, "changes": changes}


# ---------------------------------------------------------------- public API

def status(week):
    _check(week)
    got = _match(week, _read_events(week))
    out = {}
    for m in week["meetings"]:
        e = got.get(m["id"])
        out[m["id"]] = ({"start": e["start"], "end": e["end"], "date": e["date"], "title": e["title"],
                         "location": e["location"], "found": True} if e else
                        {"start": None, "end": None, "date": m["date"], "title": None, "location": None,
                         "found": False})
    return out


def push(week, sched):
    _check(week)
    known = {m["id"]: m for m in week["meetings"]}
    targets = {}
    for mid, s in sched.items():
        if mid not in known:
            raise ValueError(f"unknown meeting id {mid!r}")
        targets[mid] = _target(week, known[mid], s["start"], s.get("place"))
    return _apply(week, targets)


def reset(week):
    """Restore the week.json originals (times, locations, titles). See module docstring."""
    return _apply(week, _originals(week))


def restore_snapshot(week):
    snap = (_load_snapshot() or {}).get("events")
    if not snap:
        raise RuntimeError("no snapshot at " + str(SNAPSHOT_FILE))
    return _apply(week, {mid: {k: e[k] for k in ("title", "start", "end", "location")}
                         for mid, e in snap.items() if mid in {m["id"] for m in week["meetings"]}})


# ---------------------------------------------------------------- CLI

def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    ops = {"reset": reset, "push-original": reset, "restore-snapshot": restore_snapshot}
    if cmd not in ("status", *ops):
        print(__doc__.split("CLI:")[1].strip(), file=sys.stderr)
        return 2
    week = load_week()
    try:
        if cmd == "status":
            for mid, s in status(week).items():
                print(f"{mid:6} {s['date']} {s['start'] or '--:--'}-{s['end'] or '--:--'}  "
                      f"{s['title'] or 'NOT FOUND'}  @ {s['location'] or '-'}")
        else:
            r = ops[cmd](week)
            for line in r["changes"]:
                print(line)
            print(f"{r['updated']} event(s) changed" + (f", missing: {r['missing']}" if r["missing"] else ""))
    except (RuntimeError, ValueError) as e:
        print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
