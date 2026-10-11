#!/usr/bin/env python3
"""Sanity checks for the HomeBy4 demo data. Run: python3 data/check_week.py

Checks week.json (C1 hard limits, no overlaps incl. travel in the BEFORE schedule),
demo_week.ics (window, no attendees) and keewano_seed.jsonl (dates, shape).
Travel and "intended after" come from the solver itself (solver.metrics / solver.solve, every flexible meeting movable)
on geo.localize(week): the OSRM drive matrix when reachable or cached, else the haversine fallback. Home stays the
week.json placeholder unless $HOMEBY4_HOME is set. The expected savings below hold for both travel models.
"""
import json, os, re, sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geo, solver

HERE = os.path.dirname(os.path.abspath(__file__))
WIN = ("2027-10-11", "2027-10-15")
PHONE = "+15555550100"  # placeholder; server.py routes calls to HOMEBY4_DEMO_PHONE from .env

def mm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def hm(x):
    return f"{x // 60:02d}:{x % 60:02d}"


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def check(cond, msg):
    if not cond:
        fail(msg)


def main():
    with open(os.path.join(HERE, "week.json"), encoding="utf-8") as fh:
        week = json.load(fh)
    places, home = week["places"], week["home"]
    check(week["window"] == {"start": WIN[0], "end": WIN[1]}, "window must be 2027-10-11..2027-10-15")
    check(week["demo_phone"] == PHONE, "demo_phone")
    check(home in places, "home place missing")
    check(9 <= len(places) <= 16, f"expected ~10-14 places, got {len(places)}")
    geo.localize(week)

    ms = week["meetings"]
    ids = [m["id"] for m in ms]
    check(len(ids) == len(set(ids)), "meeting ids not unique")
    days = {}
    for m in ms:
        d = date.fromisoformat(m["date"])
        check(date.fromisoformat(WIN[0]) <= d <= date.fromisoformat(WIN[1]), f"{m['id']} date {m['date']} outside window")
        check(d.weekday() < 5, f"{m['id']} on a weekend")
        check(m["phone"] == PHONE, f"{m['id']} phone {m['phone']} != {PHONE}")
        check(m["kind"] in ("in_person", "remote"), f"{m['id']} bad kind")
        check(re.fullmatch(r"\d\d:\d\d", m["start"]) and 0 <= mm(m["start"]) < 1440, f"{m['id']} bad start")
        check(m["dur"] > 0 and mm(m["start"]) + m["dur"] <= 24 * 60, f"{m['id']} bad dur")
        check(m["note"].strip() and m["title"].strip() and m["person"].strip(), f"{m['id']} missing text")
        if m["kind"] == "remote":
            check(m["place"] is None and m["alt_places"] == [], f"{m['id']} remote must have place null, no alts")
        else:
            check(m["place"] in places, f"{m['id']} unknown place {m['place']}")
            check(m["place"] != home, f"{m['id']} meeting at home")
        for p in m["alt_places"]:
            check(p in places and p != m["place"], f"{m['id']} bad alt place {p}")
        check(len(m["alt_places"]) <= 2, f"{m['id']} more than 2 alts")
        if m["flexible"]:
            check(m["flex"], f"{m['id']} flexible but no flex windows")
            s, e = mm(m["start"]), mm(m["start"]) + m["dur"]
            check(any(mm(a) <= s and e <= mm(b) for a, b in m["flex"]), f"{m['id']} original slot outside its own flex window")
        else:
            check(m["flex"] == [] and m["alt_places"] == [], f"{m['id']} fixed meeting must have empty flex and no alts")
        days.setdefault(m["date"], []).append(m)

    check(len(days) == 5, "need all 5 weekdays")
    print(f"week.json: {len(ms)} meetings, {len(places)} places, "
          f"{sum(1 for m in ms if m['flexible'])} flexible, {sum(1 for m in ms if m['kind'] == 'remote')} remote")

    # BEFORE schedule feasibility + per-day home-at, and the solver's best AFTER (all flexible meetings movable)
    rows = {}
    for d in sorted(days):
        dm = days[d]
        check(3 <= len(dm) <= 6, f"{d}: {len(dm)} meetings (want 3-6)")
        res = solver.solve(week, d)
        before, after = res["before"], res["after"]
        check(before["feasible"], f"{d}: BEFORE schedule overlaps or lacks travel time")
        check(after["feasible"], f"{d}: AFTER (solver) infeasible")
        for mv in res["moves"]:
            m = next(x for x in dm if x["id"] == mv["id"])
            check(m["flexible"], f"{m['id']} moved but fixed")
            check(mv["to_place"] == m["place"] or mv["to_place"] in m["alt_places"], f"{m['id']} moved to a place that is not allowed")
            s0 = mm(mv["to_start"])
            check((mv["to_start"], mv["to_place"]) == (m["start"], m["place"]) or any(mm(a) <= s0 and s0 + m["dur"] <= mm(b) for a, b in m["flex"]),
                  f"{m['id']} moved outside its flex window")
        rows[d] = (before, after)
        wd = date.fromisoformat(d).strftime("%a")
        print(f"  {wd} {d}: {len(dm)} mtgs  leave {before['leave_home']}  home-at BEFORE {before['home_at']} "
              f"(travel {before['travel_min']}m, dead {before['dead_min']}m)  ->  solver AFTER {after['home_at']} "
              f"(saves {res['saved_min']}m, {len(res['moves'])} moves)")

    tb, ta = rows["2027-10-12"]
    check(mm("18:15") <= tb["home_at_min"] <= mm("18:45"), f"Tuesday before home-at {tb['home_at']} not in 18:15-18:45")
    check(mm("16:00") <= ta["home_at_min"] <= mm("16:30"), f"Tuesday solver after {ta['home_at']} not in 16:00-16:30")
    for d, (b, a) in rows.items():
        if d != "2027-10-12":
            check(30 <= b["home_at_min"] - a["home_at_min"] <= 90, f"{d}: win {b['home_at_min'] - a['home_at_min']}m not in 30-90")

    # ICS
    with open(os.path.join(HERE, "demo_week.ics"), encoding="utf-8", newline="") as fh:
        ics = fh.read()
    check("ATTENDEE" not in ics and "ORGANIZER" not in ics, "ICS contains ATTENDEE/ORGANIZER")
    check("HomeBy4 demo (synthetic)" in ics.replace("\r\n ", ""), "ICS missing synthetic marker")
    check("BEGIN:VTIMEZONE" in ics and "TZID:America/Los_Angeles" in ics, "ICS missing VTIMEZONE")
    starts = re.findall(r"^DTSTART;TZID=America/Los_Angeles:(\d{8})T(\d{4})00\r?$", ics, re.M)
    check(ics.count("BEGIN:VEVENT") == len(ms) == len(starts), "ICS event count mismatch")
    for ymd, hhmm in starts:
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        check(WIN[0] <= iso <= WIN[1], f"ICS DTSTART {iso} outside window")
    exp = sorted((m["date"].replace("-", ""), m["start"].replace(":", "")) for m in ms)
    check(sorted(starts) == exp, "ICS DTSTARTs do not match week.json (rerun data/make_ics.py)")
    print(f"demo_week.ics: {len(starts)} events, all inside window, no ATTENDEE/ORGANIZER")

    # Keewano seed (optional until generated)
    seed = os.path.join(HERE, "keewano_seed.jsonl")
    if os.path.exists(seed):
        n = {"call_outcome": 0, "day_compacted": 0}
        with open(seed, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                e = json.loads(line)
                check(e["name"] in n, f"seed line {i}: bad name")
                n[e["name"]] += 1
                check("2027-08-16" <= e["ts"][:10] <= "2027-10-08", f"seed line {i}: ts {e['ts']} outside history range")
                if e["name"] == "call_outcome":
                    for k in ("person", "meeting_title", "weekday", "proposed_shift_min", "accepted", "countered", "minutes_saved", "call_seconds"):
                        check(k in e["props"], f"seed line {i}: missing {k}")
                else:
                    for k in ("date", "weekday", "saved_min", "moves"):
                        check(k in e["props"], f"seed line {i}: missing {k}")
                    check("2027-08-16" <= e["props"]["date"] <= "2027-10-08", f"seed line {i}: date outside range")
        total = sum(n.values())
        check(150 <= total <= 250, f"seed has {total} lines (want 150-250)")
        print(f"keewano_seed.jsonl: {total} lines ({n['call_outcome']} call_outcome, {n['day_compacted']} day_compacted)")
    print("OK")


if __name__ == "__main__":
    main()
