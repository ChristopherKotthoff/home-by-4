#!/usr/bin/env python3
"""Generate data/keewano_seed.jsonl: 8 weeks of synthetic HomeBy4 negotiation history
(Mon 2027-08-16 .. Fri 2027-10-08), strictly before the demo week. Fixed seed -> re-runnable.

Built-in patterns a judge can discover with a natural-language question:
  * Dana Whitfield (angel) never accepts a morning slot, but is fine with afternoons
  * junior people accept ~85%, investors ~40%, the landlord almost never
  * Ingrid Solberg (a VC's assistant) accepts fast: shortest calls
  * Raj Patel and Nadia Haddad counter more often than they accept
  * Thursdays waste the most time: latest home-at before, biggest minutes saved
  * Fridays are easy: people say yes more, but there is little to save
Everything here is fictional.
"""
import json, os, random, sys
from datetime import date, datetime, timedelta, timezone

SEED = 200   # picked so every pattern below is clearly visible in the data
START = date(2027, 8, 16)      # Monday, 8 weeks before the demo week
END = date(2027, 10, 8)        # Friday before the demo week
SKIP = {date(2027, 9, 6), date(2027, 9, 24)}   # Labor Day, one sick day
PDT = timezone(timedelta(hours=-7))
WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# name: (role, group, base accept prob, counter prob when not accepting, call speed, weight, title verbs)
PEOPLE = {
    "Priya Nair":        ("junior designer",  "junior",   0.88, 0.30, 1.0, 12, ["Coffee", "Coffee", "Call"]),
    "Jun Takahashi":     ("junior engineer",  "junior",   0.86, 0.30, 1.0, 8,  ["Coffee", "Lunch"]),
    "Ethan Cole":        ("junior developer", "junior",   0.84, 0.30, 1.0, 8,  ["Coffee", "Call"]),
    "Mei Ling Zhou":     ("junior analyst",   "junior",   0.82, 0.35, 1.0, 6,  ["Coffee", "Lunch"]),
    "Victor Alvarez":    ("friend",           "friend",   0.74, 0.30, 0.9, 9,  ["Lunch", "Walk", "Coffee"]),
    "Marcus Lindqvist":  ("friend",           "friend",   0.76, 0.30, 0.9, 7,  ["Dinner", "Lunch"]),
    "Raj Patel":         ("vendor rep",       "vendor",   0.40, 0.75, 1.2, 11, ["Lunch", "Call", "Coffee"]),
    "Gordon Pike":       ("vendor rep",       "vendor",   0.72, 0.40, 1.0, 7,  ["Demo", "Call"]),
    "Helen Okafor":      ("recruiter",        "recruiter",0.62, 0.40, 1.1, 8,  ["Call", "Coffee"]),
    "Sofia Marchetti":   ("recruiter",        "recruiter",0.58, 0.40, 1.1, 7,  ["Lunch", "Coffee"]),
    "Tomas Ferreira":    ("mentor",           "mentor",   0.56, 0.45, 1.1, 8,  ["Breakfast", "Coffee"]),
    "Dana Whitfield":    ("angel",            "investor", 0.80, 0.65, 1.1, 14, ["Coffee", "Lunch"]),   # afternoon-only, see below
    "Nadia Haddad":      ("VC partner",       "investor", 0.36, 0.60, 1.2, 11, ["Coffee", "Lunch"]),
    "Keith Brandt":      ("landlord",         "landlord", 0.12, 0.15, 1.0, 6,  ["Coffee"]),
    "Bao Tran":          ("accountant",       "accountant",0.66, 0.35, 1.0, 6, ["Coffee", "Call"]),
    "Olivia Bennett":    ("account exec",     "sales",    0.60, 0.40, 1.0, 6,  ["Coffee", "Lunch"]),
    "Ingrid Solberg":    ("VC assistant",     "assistant",0.93, 0.15, 0.62, 9, ["Call", "Call", "Coffee"]),
}


def pick_title(rng, name, role, verbs):
    return f"{rng.choice(verbs)}: {name} ({role})"


def build(seed):
    rng = random.Random(seed)
    names = list(PEOPLE)
    weights = [PEOPLE[n][5] for n in names]
    events = []

    d = START
    while d <= END:
        if d.weekday() > 4 or d in SKIP:
            d += timedelta(days=1)
            continue
        wd = d.weekday()
        n_calls = {0: rng.randint(3, 4), 1: rng.randint(3, 5), 2: rng.randint(3, 5),
                   3: rng.randint(4, 5), 4: rng.randint(2, 3)}[wd]
        # distinct people per day, weighted
        pool, w = names[:], weights[:]
        chosen = []
        for _ in range(n_calls):
            i = rng.choices(range(len(pool)), w)[0]
            chosen.append(pool.pop(i)); w.pop(i)
        clock = datetime(d.year, d.month, d.day, 7, rng.randint(5, 35), rng.randint(0, 59), tzinfo=PDT)
        day_calls = []
        for name in chosen:
            role, group, base, cprob, speed, _, verbs = PEOPLE[name]
            orig = rng.randrange(9 * 60 + 30, 17 * 60 + 1, 15)            # original start (min of day)
            shift = -rng.randrange(30, 241, 15) if rng.random() < 0.72 else rng.randrange(15, 121, 15)
            prop = max(9 * 60 + 30, min(17 * 60 + 30, orig + shift))
            shift = prop - orig
            if shift == 0:
                shift = -30; prop = orig - 30
            morning = prop < 11 * 60 + 30
            if name == "Dana Whitfield":                                     # make her morning/afternoon split visible
                if rng.random() < 0.5:
                    prop = rng.randrange(9 * 60 + 30, 11 * 60 + 31, 15)
                else:
                    prop = rng.randrange(13 * 60, 17 * 60 + 1, 15)
                orig = max(prop + rng.randrange(45, 241, 15), 12 * 60)
                orig = min(orig, 18 * 60)
                shift = prop - orig
                morning = prop < 12 * 60
            p = base
            if name == "Dana Whitfield":
                p = 0.0 if prop < 12 * 60 else base                          # never mornings
            else:
                p -= 0.0012 * max(0, abs(shift) - 90)                        # big asks are harder
                if morning and group not in ("junior", "assistant"):
                    p -= 0.08
                if wd == 4:
                    p += 0.06                                                # Fridays: relaxed
                if wd == 3:
                    p -= 0.04
            p = max(0.02, min(0.97, p))
            accepted = rng.random() < p
            countered = (not accepted) and rng.random() < cprob
            # minutes saved by this single call, if it sticks
            gain = abs(shift) * rng.uniform(0.25, 0.60) if shift < 0 else abs(shift) * rng.uniform(0.10, 0.30)
            gain = max(10, min(90, gain)) * (1.45 if wd == 3 else 1.0)        # Thursdays hold the most compactable time
            saved = int(round(gain / 5) * 5)
            if accepted:
                minutes_saved = saved
                secs = rng.randint(38, 72)
            elif countered:
                minutes_saved = rng.choice([0, 0, 10, 15, 20]) if rng.random() < 0.6 else 0
                secs = rng.randint(66, 104)
            else:
                minutes_saved = 0
                secs = rng.randint(28, 58)
            secs = min(110, max(24, int(secs * speed)))
            clock += timedelta(seconds=rng.randint(60, 200))
            day_calls.append({
                "ts": clock.isoformat(timespec="seconds"),
                "name": "call_outcome",
                "props": {"person": name, "role": role,
                          "meeting_title": pick_title(rng, name, role, verbs),
                          "weekday": WEEKDAY[wd],
                          "proposed_start": f"{prop // 60:02d}:{prop % 60:02d}",
                          "proposed_shift_min": shift,
                          "accepted": accepted, "countered": countered,
                          "minutes_saved": minutes_saved, "call_seconds": secs}})
            clock += timedelta(seconds=secs)
        events += day_calls
        moves = sum(1 for c in day_calls if c["props"]["accepted"] or c["props"]["minutes_saved"] > 0)
        if moves:
            saved_total = sum(c["props"]["minutes_saved"] for c in day_calls)
            base_home = {0: 17 * 60 + 50, 1: 18 * 60 + 15, 2: 18 * 60 + 20, 3: 19 * 60 + 5, 4: 16 * 60 + 40}[wd]
            home_before = int(round((base_home + rng.randint(-25, 25)) / 5) * 5)
            dead_before = int(round((rng.randint(60, 110) + (35 if wd == 3 else 0)) / 5) * 5)
            clock += timedelta(seconds=rng.randint(20, 90))
            events.append({
                "ts": clock.isoformat(timespec="seconds"),
                "name": "day_compacted",
                "props": {"date": d.isoformat(), "weekday": WEEKDAY[wd], "saved_min": saved_total, "moves": moves,
                          "home_before": f"{home_before // 60:02d}:{home_before % 60:02d}",
                          "home_after": f"{(home_before - saved_total) // 60:02d}:{(home_before - saved_total) % 60:02d}",
                          "dead_min_before": dead_before}})
        d += timedelta(days=1)

    events.sort(key=lambda e: e["ts"])
    return events


def main():
    events = build(SEED)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keewano_seed.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            assert "2027-08-16" <= e["ts"][:10] <= "2027-10-08"
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"wrote {len(events)} lines to {path}")
    if "--stats" in sys.argv:
        stats(events)


def stats(events):
    calls = [e["props"] for e in events if e["name"] == "call_outcome"]
    days = [e["props"] for e in events if e["name"] == "day_compacted"]

    def rate(rows, key):
        by = {}
        for r in rows:
            by.setdefault(key(r), []).append(r["accepted"])
        return {k: f"{sum(v) / len(v):.0%} (n={len(v)})" for k, v in sorted(by.items())}
    role_group = {n: v[1] for n, v in PEOPLE.items()}
    print("accept by group:", rate(calls, lambda r: role_group[r["person"]]))
    print("accept by person:", rate(calls, lambda r: r["person"]))
    dana = [c for c in calls if c["person"] == "Dana Whitfield"]
    print("Dana morning:", [(c["proposed_start"], c["accepted"]) for c in dana if c["proposed_start"] < "12:00"][:12])
    print("Dana afternoon accept:", sum(c["accepted"] for c in dana if c["proposed_start"] >= "12:00"),
          "of", sum(1 for c in dana if c["proposed_start"] >= "12:00"))
    print("counter rate:", {n: f"{sum(c['countered'] for c in calls if c['person'] == n)}/{sum(1 for c in calls if c['person'] == n)}"
                            for n in ("Raj Patel", "Nadia Haddad", "Dana Whitfield")})
    wsum = {}
    for r in days:
        wsum.setdefault(r["weekday"], []).append(r["saved_min"])
    print("avg saved_min per compacted day:", {k: round(sum(v) / len(v)) for k, v in wsum.items()})
    hb = {}
    for r in days:
        hb.setdefault(r["weekday"], []).append(int(r["home_before"][:2]) * 60 + int(r["home_before"][3:]))
    print("avg home_before:", {k: f"{sum(v) // len(v) // 60}:{sum(v) // len(v) % 60:02d}" for k, v in hb.items()})
    sec = {}
    for c in calls:
        sec.setdefault(c["person"], []).append(c["call_seconds"])
    print("avg call_seconds:", {k: round(sum(v) / len(v)) for k, v in sorted(sec.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))[:3]})


if __name__ == "__main__":
    main()
