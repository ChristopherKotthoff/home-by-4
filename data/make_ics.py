#!/usr/bin/env python3
"""Generate data/demo_week.ics from data/week.json (re-runnable, deterministic).

Safety: no ATTENDEE / ORGANIZER lines, so importing the file never emails anyone.
Hard limit: every event must fall inside the window in week.json (2027-10-11..2027-10-15).
"""
import json, os
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
HARD_START, HARD_END = date(2027, 10, 11), date(2027, 10, 15)

VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
TZNAME:PDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
TZNAME:PST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE""".split("\n")


def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """RFC 5545 line folding at 75 octets (UTF-8 aware)."""
    out, cur, n = [], "", 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if n + b > (75 if not out else 74):
            out.append(cur)
            cur, n = "", 0
        cur += ch
        n += b
    out.append(cur)
    return "\r\n ".join(out)


def build(week, confirmed=None):
    """ICS text for the week; `confirmed` (C2 shape) applies agreed fixed_start/places, so re-importing updates events."""
    confirmed = confirmed or {}
    places = week["places"]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HomeBy4//demo week//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:HomeBy4 demo week",
             "X-WR-TIMEZONE:America/Los_Angeles"] + VTIMEZONE
    for m in week["meetings"]:
        c = confirmed.get(m["id"]) or {}
        moved = "fixed_start" in c and (c["fixed_start"] != m["start"] or (c.get("places") or [m["place"]])[0] != m["place"])
        if moved:
            m = dict(m, start=c["fixed_start"], place=(c.get("places") or [m["place"]])[0] if m["place"] else None,
                     note="Moved by HomeBy4 after a phone call. " + m["note"])
        d = date.fromisoformat(m["date"])
        if not (HARD_START <= d <= HARD_END):
            raise ValueError(f"{m['id']}: {m['date']} outside demo window")
        start = datetime.fromisoformat(f"{m['date']}T{m['start']}:00")
        end = start + timedelta(minutes=m["dur"])
        loc = places[m["place"]]["name"] if m["place"] else "Phone / video call"
        desc = (f"HomeBy4 demo (synthetic). Fictional person: {m['person']}. "
                f"{'Flexible' if m['flexible'] else 'Fixed'} meeting. {m['note']}")
        f = "%Y%m%dT%H%M%S"
        lines += ["BEGIN:VEVENT",
                  f"UID:{m['id']}@homeby4.demo",
                  "DTSTAMP:20270901T000000Z", f"SEQUENCE:{1 if moved else 0}",
                  f"DTSTART;TZID=America/Los_Angeles:{start.strftime(f)}",
                  f"DTEND;TZID=America/Los_Angeles:{end.strftime(f)}",
                  f"SUMMARY:{esc(m['title'])}",
                  f"LOCATION:{esc(loc)}",
                  f"DESCRIPTION:{esc(desc)}",
                  "STATUS:CONFIRMED", "TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(fold(l) for l in lines) + "\r\n"
    assert "ATTENDEE" not in body and "ORGANIZER" not in body
    return body


def main():
    with open(os.path.join(HERE, "week.json"), encoding="utf-8") as fh:
        week = json.load(fh)
    body = build(week)
    with open(os.path.join(HERE, "demo_week.ics"), "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    print(f"wrote demo_week.ics with {len(week['meetings'])} events")


if __name__ == "__main__":
    main()
