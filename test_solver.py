"""Assert-based tests for solver.py. Run: python3 test_solver.py"""
import itertools
import math
import random
import time

import solver as S

D = "2027-10-12"

PLACES = {
    "home":      {"name": "Home · Noe Valley",   "lat": 37.7502, "lng": -122.4337},
    "ritual":    {"name": "Ritual Coffee",       "lat": 37.7565, "lng": -122.4213},
    "sightglass": {"name": "Sightglass SoMa",    "lat": 37.7708, "lng": -122.4090},
    "office":    {"name": "Office SoMa",         "lat": 37.7786, "lng": -122.3893},
    "hayes":     {"name": "Hayes Valley Cafe",   "lat": 37.7766, "lng": -122.4241},
    "dolores":   {"name": "Dolores Park Cafe",   "lat": 37.7596, "lng": -122.4269},
    "fidi":      {"name": "FiDi Tower",          "lat": 37.7946, "lng": -122.3999},
}


def mk(id, start, dur, place, flex=None, alts=(), kind="in_person", date=D, person=None):
    return {"id": id, "date": date, "start": start, "dur": dur, "title": "T " + id,
            "person": person or "P " + id, "phone": "+15555550100", "kind": kind,
            "place": None if kind == "remote" else place, "alt_places": list(alts),
            "flexible": flex is not None, "flex": flex or [], "note": ""}


def week(meetings):
    return {"owner": "Chris", "tz": "America/Los_Angeles",
            "window": {"start": "2027-10-11", "end": "2027-10-15"},
            "demo_phone": "+15555550100", "home": "home", "places": PLACES, "meetings": meetings}


def tuesday():
    return week([
        mk("tue-1", "09:30", 45, "ritual", [["08:30", "17:00"]], ["sightglass"]),
        mk("tue-2", "11:00", 90, "office"),
        mk("tue-3", "13:15", 60, "hayes", [["11:30", "15:00"]], ["office"]),
        mk("tue-4", "15:00", 60, "ritual", [["10:00", "17:00"]], ["dolores"]),
        mk("tue-5", "17:30", 45, "fidi"),       # 17:30: the rush-hour drive from Ritual must fit after tue-6
        mk("tue-6", "16:15", 30, None, [["14:00", "17:00"]], kind="remote"),
    ])


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lng"], b["lat"], b["lng"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def key(r):
    a = r["after"]
    return (a["home_at_min"], a["travel_min"] + a["dead_min"], len(r["moves"]))


# ------------------------------------------------------------------ travel
def test_travel():
    w = week([])
    assert S.travel_min(w, "home", "home") == 0
    assert S.travel_min(w, None, "home") == 0 and S.travel_min(w, "home", None) == 0
    for a, b in itertools.permutations(PLACES, 2):
        t = S.travel_min(w, a, b)
        raw = hav(PLACES[a], PLACES[b]) * 1.35 / 20 * 60 + 6
        assert t % 5 == 0 and raw <= t < raw + 5, (a, b, t, raw)
        assert t == S.travel_min(w, b, a)
    assert S.travel_min(w, "home", "fidi") > S.travel_min(w, "home", "ritual")


def test_travel_uses_matrix():
    w = week([])
    off = S.travel_min(w, "home", "fidi")
    w["drive_min"] = {"home|fidi": 12.0}                     # OSRM minutes, one direction only
    assert S.travel_min(w, "home", "fidi") == 20 and S.travel_min(w, "home", "fidi", depart_min=8 * 60) == 25   # ceil5(12 + 6), ceil5(12 * 1.35 + 6)
    assert S.travel_min(w, "fidi", "home") == S.travel_min(week([]), "fidi", "home") and off != 20             # other pairs: haversine
    assert S._tables(w, ["home", "fidi"])[0]["home"]["fidi"] == 20


def test_travel_rush_hour():
    w = week([])
    a, b = "home", "fidi"
    base = S.travel_min(w, a, b)
    drive = hav(PLACES[a], PLACES[b]) * 1.35 / 20 * 60
    rush = int(math.ceil((drive * 1.35 + 6) / 5 - 1e-9) * 5)
    assert rush > base
    for t, want in (("07:59", base), ("08:00", rush), ("09:29", rush), ("09:30", base), ("12:00", base),
                    ("16:29", base), ("16:30", rush), ("18:29", rush), ("18:30", base)):
        assert S.travel_min(w, a, b, depart_min=S._mn(t)) == want, (t, want)
    assert S.travel_min(w, a, b) == base and S.travel_min(w, a, a, depart_min=8 * 60) == 0
    assert S.travel_min(w, None, b, depart_min=8 * 60) == 0
    # a leg into a meeting leaves as late as possible: rush is looked up at the off-peak departure time
    assert S._travel_to(w, a, b, 9 * 60 + 30 + base) == base          # off-peak departure 09:30 -> off-peak
    assert S._travel_to(w, a, b, 9 * 60 + 29 + base) == rush          # off-peak departure 09:29 -> rush
    # off-peak travel obeys the triangle inequality: the pairwise pruning in _search relies on it
    import json
    with open("data/week.json") as f:
        real = json.load(f)
    for ww in (w, real):
        ps = list(ww["places"])
        for x, y, z in itertools.permutations(ps, 3):
            assert S.travel_min(ww, x, z) <= S.travel_min(ww, x, y) + S.travel_min(ww, y, z), (x, y, z)
            assert S.travel_min(ww, x, y, depart_min=8 * 60) >= S.travel_min(ww, x, y)


# ------------------------------------------------------------------ metrics
def sched(w, d=D):
    return [{"id": m["id"], "start": m["start"], "place": m["place"]} for m in w["meetings"] if m["date"] == d]


def test_metrics_basic():
    w = tuesday()
    m = S.metrics(w, D, sched(w))
    assert m["feasible"], m["conflicts"]
    t_home_ritual = S.travel_min(w, "home", "ritual", depart_min=9 * 60 + 30 - S.travel_min(w, "home", "ritual"))
    assert m["leave_home_min"] == 9 * 60 + 30 - t_home_ritual
    assert m["leave_home"] == S._hm(m["leave_home_min"])
    # last commitment: 17:30-18:15 FiDi, home leg departs 18:15 (rush hour)
    assert m["home_at_min"] == 18 * 60 + 15 + S.travel_min(w, "fidi", "home", depart_min=18 * 60 + 15)
    assert S.travel_min(w, "fidi", "home", depart_min=18 * 60 + 15) > S.travel_min(w, "fidi", "home")
    assert [s["rush"] for s in m["segments"] if s["type"] == "travel"][-1] is True
    assert m["home_at"] == S._hm(m["home_at_min"])
    # timeline closes: segments are contiguous and cover leave_home..home_at
    segs = m["segments"]
    assert segs[0]["start_min"] == m["leave_home_min"] and segs[-1]["end_min"] == m["home_at_min"]
    for a, b in zip(segs, segs[1:]):
        assert a["end_min"] <= b["start_min"], (a, b)
    assert sum(s["min"] for s in segs) == m["home_at_min"] - m["leave_home_min"]
    for typ, key_ in (("travel", "travel_min"), ("dead", "dead_min"), ("free", "free_min")):
        assert sum(s["min"] for s in segs if s["type"] == typ) == m[key_]
    assert all(s["type"] in ("meeting", "travel", "dead", "free") for s in segs)
    for s in segs:
        if s["type"] == "travel":
            assert s["from"] != s["to"]
        if s["type"] == "meeting":
            assert "id" in s
        if s["type"] in ("dead", "free"):
            assert (s["min"] < S.DEAD_GAP_MAX) == (s["type"] == "dead")
    assert [e["id"] for e in m["events"]] == ["tue-1", "tue-2", "tue-3", "tue-4", "tue-6", "tue-5"]


def test_metrics_dead_vs_free_and_remote():
    # two in-person at same place: gap 40 -> dead, gap 45 -> free (no travel in between)
    for gap, typ in ((40, "dead"), (44, "dead"), (45, "free"), (0, None)):
        w = week([mk("a", "10:00", 60, "office"), mk("b", S._hm(11 * 60 + gap), 30, "office")])
        m = S.metrics(w, D, sched(w))
        got = [s["type"] for s in m["segments"] if s["type"] in ("dead", "free")]
        assert got == ([typ] if typ else []), (gap, got)
        assert m["dead_min"] == (gap if typ == "dead" else 0)
        assert m["free_min"] == (gap if typ == "free" else 0)
    # gap counted after travel
    w = week([mk("a", "10:00", 60, "office"), mk("b", "12:00", 30, "dolores")])
    t = S.travel_min(w, "office", "dolores", depart_min=12 * 60 - S.travel_min(w, "office", "dolores"))
    m = S.metrics(w, D, sched(w))
    assert m["dead_min"] + m["free_min"] == 60 - t
    # remote first: you are at home, no travel to it; remote in the middle stays where you are
    w = week([mk("r1", "08:00", 30, None, kind="remote"), mk("a", "09:30", 60, "office"),
              mk("r2", "10:45", 30, None, kind="remote"), mk("b", "12:00", 30, "office")])
    m = S.metrics(w, D, sched(w))
    assert m["feasible"]
    segs = m["segments"]
    assert segs[0]["type"] == "meeting" and segs[0]["id"] == "r1" and m["leave_home"] == "08:00"
    tr = [s for s in segs if s["type"] == "travel"]
    assert tr[0]["from"] == "home" and tr[0]["to"] == "office" and tr[0]["end"] == "09:30"
    assert len(tr) == 2 and tr[1]["from"] == "office" and tr[1]["to"] == "home"   # none for r2 / b
    assert m["home_at_min"] == 12 * 60 + 30 + S.travel_min(w, "office", "home", depart_min=12 * 60 + 30)
    # all-remote day: no travel
    w = week([mk("r", "10:00", 30, None, kind="remote")])
    m = S.metrics(w, D, sched(w))
    assert m["travel_min"] == 0 and m["home_at"] == "10:30"
    # overlap flagged
    w = week([mk("a", "10:00", 60, "office"), mk("b", "10:30", 30, "dolores")])
    assert not S.metrics(w, D, sched(w))["feasible"]
    # empty day
    m = S.metrics(week([]), D, [])
    assert m["home_at"] is None and m["segments"] == [] and m["travel_min"] == 0


# ------------------------------------------------------------------ solve
def check_solution(w, res):
    a = res["after"]
    assert a["feasible"], a["conflicts"]
    ids = {m["id"] for m in w["meetings"] if m["date"] == res["date"]}
    assert {e["id"] for e in a["events"]} == ids
    mm = {m["id"]: m for m in w["meetings"]}
    ev = {e["id"]: e for e in a["events"]}
    for i in ids:
        if not mm[i]["flexible"]:
            assert ev[i]["start"] == mm[i]["start"] and ev[i]["place"] == mm[i]["place"], i
    for mv in res["moves"]:
        assert mm[mv["id"]]["flexible"]
        assert mv["person"] == mm[mv["id"]]["person"]
        assert (mv["from_start"], mv["from_place"]) != (mv["to_start"], mv["to_place"])
        assert ev[mv["id"]]["start"] == mv["to_start"] and ev[mv["id"]]["place"] == mv["to_place"]
        assert mv["to_place"] in [mm[mv["id"]]["place"]] + mm[mv["id"]]["alt_places"] or mm[mv["id"]]["kind"] == "remote"


def test_solve_tuesday():
    w = tuesday()
    r = S.solve(w, D)
    check_solution(w, r)
    b, a = r["before"], r["after"]
    assert set(r) >= {"date", "before", "after", "moves", "saved_min", "potential"}
    assert r["saved_min"] == b["home_at_min"] - a["home_at_min"]
    assert r["saved_min"] == 0 or r["potential"] is True
    assert (a["home_at_min"], a["travel_min"] + a["dead_min"]) <= (b["home_at_min"], b["travel_min"] + b["dead_min"])
    assert a["travel_min"] + a["dead_min"] < b["travel_min"] + b["dead_min"] and r["moves"]
    assert r["potential"] is True
    for mv in r["moves"]:
        assert set(mv) == {"id", "person", "from_start", "to_start", "from_place", "to_place", "why"}
        assert isinstance(mv["why"], str) and mv["why"].endswith(".") and len(mv["why"]) > 10
    # fixed tue-5 at 17:30 FiDi is last: home_at can't improve, but travel/dead does
    assert a["home_at_min"] == b["home_at_min"]


def test_solve_home_earlier():
    # one fixed meeting early, one flexible late -> pull the late one forward, get home earlier
    w = week([mk("a", "09:00", 60, "office"), mk("b", "17:00", 60, "office", [["09:00", "18:00"]])])
    r = S.solve(w, D)
    check_solution(w, r)
    assert r["saved_min"] > 0 and [mv["id"] for mv in r["moves"]] == ["b"]
    assert r["moves"][0]["to_start"] == "10:00" and r["potential"] is True
    # confirmations
    r2 = S.solve(w, D, {"b": {"fixed_start": "10:00", "places": ["office"]}})
    assert r2["potential"] is False and r2["moves"][0]["to_start"] == "10:00"
    r3 = S.solve(w, D, {"b": {"fixed_start": "17:00", "places": ["office"]}})   # declined
    assert r3["moves"] == [] and r3["saved_min"] == 0 and r3["potential"] is False
    r4 = S.solve(w, D, {"b": {"windows": [["13:00", "15:00"]]}})                  # countered
    assert r4["moves"][0]["to_start"] == "13:00" and r4["potential"] is False
    check_solution(w, r4)
    r5 = S.solve(w, D, {"b": {"windows": [["13:10", "14:20"]]}})                  # 15-min grid, fits fully
    assert r5["moves"][0]["to_start"] == "13:15", r5["moves"]
    r6 = S.solve(w, D, {"b": {"windows": [["13:10", "14:10"]]}})                  # 60 min can't fit on grid
    assert r6["moves"] == [] or r6["moves"][0]["to_start"] != "13:15"
    # nothing flexible
    w2 = week([mk("a", "09:00", 60, "office"), mk("b", "17:00", 60, "ritual")])
    r7 = S.solve(w2, D)
    assert r7["moves"] == [] and r7["saved_min"] == 0 and r7["potential"] is False
    # contradictory confirmation does not crash
    r8 = S.solve(w, D, {"b": {"fixed_start": "09:30", "places": ["office"]}})
    assert r8["moves"] == [] and r8["saved_min"] == 0   # falls back to the original day


def test_solve_week_and_limits():
    w = tuesday()
    w["meetings"].append(mk("mon-1", "10:00", 30, "office", [["09:00", "12:00"]], date="2027-10-11"))
    sw = S.solve_week(w)
    assert list(sw) == ["2027-10-11", "2027-10-12", "2027-10-13", "2027-10-14", "2027-10-15"]
    assert sw["2027-10-13"]["after"]["home_at"] is None and sw["2027-10-13"]["saved_min"] == 0
    assert sw["2027-10-12"]["moves"] == S.solve(w, D)["moves"]
    for bad in ("2027-10-10", "2027-10-16", "2026-01-01"):
        try:
            S.solve(w, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)


def spec_slots(m, conf):
    """Allowed (start "HH:MM", place) for one meeting per C2, written independently of solver._cands."""
    orig = (m["start"], None if m["kind"] == "remote" else m["place"])
    if not m["flexible"]:
        return [orig]
    c = conf.get(m["id"]) or {}
    places = [None] if m["kind"] == "remote" else (c.get("places") or [m["place"]] + m["alt_places"])
    if "fixed_start" in c:
        return sorted({(c["fixed_start"], p) for p in places}, key=str)
    wins = c.get("windows") or m["flex"]
    starts = {s for w0, w1 in wins for s in range(0, 24 * 60, 15) if S._mn(w0) <= s and s + m["dur"] <= S._mn(w1)}
    return sorted({(S._hm(s), p) for s in starts for p in places} | {orig}, key=str)


def brute(w, d, conf=None):
    """Exhaustive enumeration of every allowed combination, scored only by metrics(); best objective tuple."""
    conf = conf or {}
    ms = [m for m in w["meetings"] if m["date"] == d]
    base = {m["id"]: (m["start"], S._place(m)) for m in ms}
    best = None
    for combo in itertools.product(*[spec_slots(m, conf) for m in ms]):
        sc = [{"id": m["id"], "start": st, "place": p} for m, (st, p) in zip(ms, combo)]
        mt = S.metrics(w, d, sc)
        if not mt["feasible"]:
            continue
        moved = sum(1 for m, c in zip(ms, combo) if c != base[m["id"]])
        k = (mt["home_at_min"], mt["travel_min"] + mt["dead_min"], moved)
        best = k if best is None or k < best else best
    return best


def n_combos(w, d, conf):
    n = 1
    for m in w["meetings"]:
        if m["date"] == d:
            n *= len(spec_slots(m, conf or {}))
    return n


def check_allowed(w, res, conf):
    ev = {e["id"]: e for e in res["after"]["events"]}
    for m in w["meetings"]:
        if m["date"] == res["date"]:
            assert (ev[m["id"]]["start"], ev[m["id"]]["place"]) in spec_slots(m, conf or {}), (m["id"], ev[m["id"]])


def random_day(rnd):
    pl = ["ritual", "sightglass", "office", "hayes", "dolores", "fidi"]
    n = rnd.randint(3, 6)
    ms, t = [], rnd.choice([7 * 60 + 30, 8 * 60, 8 * 60 + 30, 14 * 60, 15 * 60 + 30])   # cross rush hours
    for i in range(n):
        dur = rnd.choice([30, 45, 60])
        st = t + rnd.choice([15, 30, 45, 60])
        t = st + dur
        kind = "remote" if rnd.random() < 0.2 else "in_person"
        p = rnd.choice(pl)
        if rnd.random() < 0.6:
            lo = max(7 * 60, st - rnd.choice([30, 60, 90]))
            hi = min(20 * 60, st + dur + rnd.choice([30, 60, 90]))
            ms.append(mk("m%d" % i, S._hm(st), dur, p, [[S._hm(lo), S._hm(hi)]],
                         rnd.sample([x for x in pl if x != p], rnd.randint(0, 2)), kind=kind))
        else:
            ms.append(mk("m%d" % i, S._hm(st), dur, p, kind=kind))
    return week(ms)


def random_conf(rnd, w):
    conf = {}
    for m in w["meetings"]:
        if not m["flexible"] or rnd.random() < 0.6:
            continue
        r = rnd.random()
        places = [] if m["kind"] == "remote" else rnd.sample([m["place"]] + m["alt_places"], 1)
        if r < 0.4:      # countered with a narrower window
            a, b = S._mn(m["flex"][0][0]), S._mn(m["flex"][0][1])
            mid = a + (b - a) // 2
            conf[m["id"]] = {"windows": [[S._hm(a), S._hm(mid + 15)]], "places": places}
        elif r < 0.7:    # accepted some slot (possibly off-grid or infeasible)
            conf[m["id"]] = {"fixed_start": S._hm(S._mn(m["start"]) + rnd.choice([-35, -30, 0, 30, 60])),
                             "places": places}
        else:            # declined: pinned to the original
            conf[m["id"]] = {"fixed_start": m["start"], "places": [] if m["kind"] == "remote" else [m["place"]]}
    return conf or None


def test_vs_bruteforce():
    """Seeded random days (3-6 meetings, rush hours, remote, confirmations): solver == exhaustive enumeration."""
    rnd = random.Random(7)
    done = combos = infeasible = 0
    while done < 60:
        w = random_day(rnd)
        if not S.metrics(w, D, sched(w))["feasible"]:
            continue
        conf = random_conf(rnd, w) if rnd.random() < 0.5 else None
        n = n_combos(w, D, conf)
        if n > 6000:
            continue
        r = S.solve(w, D, conf)
        check_solution(w, r)
        best = brute(w, D, conf)
        if best is None:      # contradictory confirmations: the solver keeps the original day
            assert r["moves"] == [] and r["after"]["segments"] == r["before"]["segments"]
            infeasible += 1
        else:
            check_allowed(w, r, conf)
            assert key(r) == best, (done, key(r), best, conf)
        done += 1
        combos += n
    assert infeasible < 30
    print("bruteforce: 60 random days (%d with contradictory confirmations), %d combinations enumerated, "
          "solver matched every optimum" % (infeasible, combos))


def test_original_slot_and_windows():
    # original slot is always allowed, even when no window slot can work
    w = week([mk("a", "10:00", 120, "office"), mk("b", "14:00", 60, "office", [["10:00", "12:00"]])])
    r = S.solve(w, D)
    assert r["moves"] == [] and r["after"]["feasible"]
    # countered windows: the meeting fits inside them (start >= a, start + dur <= b) or stays at its original slot
    w = week([mk("a", "09:00", 60, "office"), mk("b", "17:00", 60, "office", [["09:00", "18:00"]]),
              mk("c", "12:00", 30, "dolores", [["09:00", "18:00"]])])
    for win in (["10:20", "11:50"], ["13:00", "13:59"], ["15:00", "16:00"]):
        conf = {"b": {"windows": [win]}}
        r = S.solve(w, D, conf)
        check_allowed(w, r, conf)
        st = next(e for e in r["after"]["events"] if e["id"] == "b")["start_min"]
        assert st == 17 * 60 or (S._mn(win[0]) <= st and st + 60 <= S._mn(win[1])), (win, st)


def real_week():
    import json
    with open("data/week.json") as f:
        return json.load(f)


def test_demo_tuesday_real_data():
    w = real_week()
    r = S.solve(w, D)
    check_solution(w, r)
    b, a = r["before"], r["after"]
    assert b["home_at"] == "18:40" and a["home_at"] == "16:15", (b["home_at"], a["home_at"])   # rush-hour model
    mv = {m["id"]: m for m in r["moves"]}
    assert mv["tue-5"]["to_place"] == "ritual" and mv["tue-5"]["to_start"] < "15:15"
    assert key(r) == brute(w, D)                              # the real demo day is provably optimal too


def test_rank_asks():
    w = real_week()
    r = S.solve(w, D)
    ao = r["ask_order"]
    assert ao == S.rank_asks(w, D)
    ids = [a["id"] for a in ao]
    assert sorted(ids) == ["tue-1", "tue-3", "tue-5"] and ids[0] == "tue-5"
    for a in ao:
        assert set(a) >= {"id", "person", "saved_if_yes_min", "if_no_min", "skip"}
    vals = [(a["saved_if_yes_min"], a["if_no_min"]) for a in ao]
    assert vals == sorted(vals, reverse=True)
    olin, raj, jun = (next(a for a in ao if a["id"] == i) for i in ("tue-5", "tue-3", "tue-1"))
    assert olin["saved_if_yes_min"] >= S.ASK_MIN_SAVED and not olin["skip"]
    assert jun["skip"] and jun["saved_if_yes_min"] < S.ASK_MIN_SAVED
    assert raj["skip"]        # on the real venue coordinates Raj no longer needs to move (he used to pay off with Olin)
    # definition, checked by brute force: others pinned at their slot, this one person free
    ms = [m for m in w["meetings"] if m["date"] == D and m["flexible"]]
    pins = {m["id"]: S._pin(m) for m in ms}
    none = brute(w, D, pins)[0]
    for a in ao:
        yes = brute(w, D, {k: v for k, v in pins.items() if k != a["id"]})[0]
        assert a["saved_if_yes_min"] == none - yes, a
    # Olin answered (13:00-15:00 at Ritual): he is no longer in the ask order
    conf = {"tue-5": {"windows": [["13:00", "15:00"]], "places": ["ritual"]}}
    ao2 = S.rank_asks(w, D, conf)
    r2 = S.solve(w, D, conf)
    assert ao2 == r2["ask_order"] and "tue-5" not in [a["id"] for a in ao2]
    # everyone answered: nobody left to call
    full = {i: {"fixed_start": m["start"], "places": [m["place"]]} for i, m in
            ((m["id"], m) for m in ms)}
    assert S.rank_asks(w, D, full) == []
    try:
        S.rank_asks(w, "2027-10-16")
    except ValueError:
        pass
    else:
        raise AssertionError("date limit")


def test_why():
    w = real_week()
    r = S.solve(w, D)
    why = {m["id"]: m["why"] for m in r["moves"]}
    assert "home %s earlier" % S._span(r["saved_min"]) in why["tue-5"], why
    assert "Lena Hoffmann at 3:15 pm" in why["tue-5"] and "Ritual" in why["tue-5"]
    for d, res in S.solve_week(w).items():
        for mv in res["moves"]:
            assert mv["why"][0].isupper() and mv["why"].endswith(".") and "None" not in mv["why"], (d, mv)
    th = {m["id"]: m["why"] for m in S.solve(w, "2027-10-14")["moves"]}
    assert th["thu-6"].startswith("Together with moving Keith Brandt"), th


def test_timing():
    # worst case of C2: 6 meetings, 3 flexible with ~40 starts x 3 places each. The last meeting is fixed, so
    # home_at cannot separate plans and the search must settle travel+dead across all of them.
    for last_flex in (False, True):
        w = week([
            mk("a", "09:00", 45, "ritual", [["08:00", "18:15"]], ["sightglass", "dolores"]),
            mk("b", "10:30", 60, "office"),
            mk("c", "12:30", 45, "hayes", [["08:00", "18:15"]], ["office", "sightglass"]),
            mk("d", "14:00", 45, "dolores", None if last_flex else [["08:00", "18:15"]], ["ritual", "hayes"]),
            mk("e", "15:30", 60, "fidi"),
            mk("f", "18:15", 30, "office", [["08:00", "19:00"]] if last_flex else None),
        ])
        stats = {}
        t0 = time.perf_counter()
        r = S.solve(w, D)
        dt = time.perf_counter() - t0
        S._search(w, w["meetings"], {}, stats)
        check_solution(w, r)
        print("timing: 6 meetings / 3 flexible x ~117 slots (last %s): %.3f s incl. ask ranking, %d nodes" % (
            "flexible" if last_flex else "fixed", dt, stats["nodes"]))
        assert dt < 2.0, dt
    w = real_week()
    t0 = time.perf_counter()
    for d in ("2027-10-11", "2027-10-12", "2027-10-13", "2027-10-14", "2027-10-15"):
        t1 = time.perf_counter()
        S.solve(w, d)
        assert time.perf_counter() - t1 < 0.3, d
    print("timing: data/week.json whole week (5 days, 19 flexible, with ask_order + why): %.3f s" % (
        time.perf_counter() - t0))


def with_engine(name, fn, *a):
    import os
    old = os.environ.get("HOMEBY4_ENGINE")
    os.environ["HOMEBY4_ENGINE"] = name
    try:
        return fn(*a)
    finally:
        if old is None:
            del os.environ["HOMEBY4_ENGINE"]
        else:
            os.environ["HOMEBY4_ENGINE"] = old


def test_engines_agree():
    """Seeded random days: dp == bnb (identical solve() output) == exhaustive enumeration (objective)."""
    rnd = random.Random(2027)
    done = 0
    while done < 40:
        w = random_day(rnd)
        if not S.metrics(w, D, sched(w))["feasible"]:
            continue
        conf = random_conf(rnd, w) if rnd.random() < 0.5 else None
        if n_combos(w, D, conf) > 6000:
            continue
        rb = with_engine("bnb", S.solve, w, D, conf)
        rd = with_engine("dp", S.solve, w, D, conf)
        assert rb == rd, (done, rb["moves"], rd["moves"], conf)
        best = brute(w, D, conf)
        assert best is None or key(rd) == best, (done, key(rd), best)
        done += 1
    # the real week, every day, with and without answers
    w = real_week()
    tue = {"tue-5": {"windows": [["13:00", "15:00"]], "places": ["ritual"]}}
    for d in ("2027-10-11", "2027-10-12", "2027-10-13", "2027-10-14", "2027-10-15"):
        for conf in (None, tue):
            assert with_engine("bnb", S.solve, w, d, conf) == with_engine("dp", S.solve, w, d, conf), d
    # engine is read at call time; unknown names fail loudly
    for bad in ("greedy", "BNBX"):
        try:
            with_engine(bad, S.solve, w, D)
        except ValueError as e:
            assert "bnb" in str(e) and "dp" in str(e)
        else:
            raise AssertionError(bad)
    assert with_engine("DP", S.solve, w, D)["after"]["home_at"] == S.solve(w, D)["after"]["home_at"]


def test_timing_engines():
    w = real_week()
    days = ("2027-10-11", "2027-10-12", "2027-10-13", "2027-10-14", "2027-10-15")
    worst = week([
        mk("a", "09:00", 45, "ritual", [["08:00", "18:15"]], ["sightglass", "dolores"]),
        mk("b", "10:30", 60, "office"),
        mk("c", "12:30", 45, "hayes", [["08:00", "18:15"]], ["office", "sightglass"]),
        mk("d", "14:00", 45, "dolores", [["08:00", "18:15"]], ["ritual", "hayes"]),
        mk("e", "15:30", 60, "fidi"),
        mk("f", "18:15", 30, "office"),
    ])
    assert with_engine("bnb", S.solve, worst, D) == with_engine("dp", S.solve, worst, D)
    for name in sorted(S.ENGINES):
        t0 = time.perf_counter()
        for d in days:
            t1 = time.perf_counter()
            with_engine(name, S.solve, w, d)
            assert time.perf_counter() - t1 < 0.3, (name, d)
        tw = time.perf_counter() - t0
        t0 = time.perf_counter()
        with_engine(name, S.solve, worst, D)
        tx = time.perf_counter() - t0
        print("timing[%s]: demo week (5 days, solve + ask_order + why) %.3f s; worst case 6 mtg / 3 flex x 117 slots"
              " %.3f s" % (name, tw, tx))
        assert tx < 2.0, (name, tx)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("ALL %d TESTS PASSED" % n)
