"""HomeBy4 solver (contract C2). Python stdlib only, pure functions.

travel_min(week, a, b, depart_min=None) -> int minutes (rush-hour aware when depart_min is given)
metrics(week, date, sched)              -> dict (timeline, travel, dead, home_at, segments)
solve(week, date, confirmed=None)       -> {date, before, after, moves, saved_min, potential, ask_order}
                                           every move also carries a plain-English "why"
rank_asks(week, date, confirmed=None)   -> [{id, person, saved_if_yes_min, if_no_min, travel_dead_saved_min, skip}]
solve_week(week, confirmed=None)        -> {date: solve(...)}
Engine: $HOMEBY4_ENGINE = "bnb" (default, branch and bound) | "dp" (dynamic programme); both exact, same output.

Travel model (defined here and nowhere else)
  base drive = week["drive_min"]["a|b"] when present (real OSRM driving minutes, free flow, set by geo.localize; the
  pair may be asymmetric), else haversine_km * 1.35 (street detour) / 20 km/h * 60 (tests, offline). Then
  drive * RUSH_FACTOR (1.35) when the leg departs
  inside a RUSH window (08:00-09:30 and 16:30-18:30, start inclusive, end exclusive); then + 6 min parking,
  rounded up to 5 min. depart_min=None gives the off-peak time, which is a lower bound for any departure.
  A leg into a meeting leaves as late as possible: the rush lookup uses its off-peak departure time
  (start - off-peak travel). That is the latest self-consistent departure whenever one exists; if rush hour
  starts during the off-peak trip, the leg is charged at the rush rate. The leg home departs when the last
  meeting ends. Remote meetings need no travel and happen wherever you are (you cannot drive during them).

Search (exact; both engines cross-checked against exhaustive enumeration in test_solver.py)
  Fixed meetings stay put. A flexible meeting's candidates are 15-min grid starts with the whole meeting
  inside its flex windows (or its confirmed windows), times {place} + alt_places (or the confirmed places),
  plus its original slot, which is always allowed because it was agreed before. confirmed fixed_start pins it.
  Objective, minimised lexicographically: (home_at, travel_min + dead_min, #moves, total minutes shifted).
  Depth-first branch and bound over the flexible meetings (fewest candidates first):
    * pairwise masks: a candidate survives only if it fits next to every fixed meeting and some candidate of
      every other flexible meeting (no overlap, off-peak travel fits). Off-peak travel obeys the triangle
      inequality and rush only adds time, so this is a relaxation: it never removes a feasible plan.
    * bound: home_at >= end + off-peak travel home of every chosen or still-possible candidate; candidates are
      sorted by that bound, so a branch stops as soon as its bound exceeds the best home_at found so far.
    * leaves are evaluated exactly (rush-hour travel, dead time) by the same rules as metrics().
  Engine "dp" (see _search_dp): DP over (meetings placed, last end minute, current place) in time order.
  Final tie-break in both: slot rank by (start, place) per meeting, meetings in id order.
"""
import json
import math
import sys
from datetime import date as _date, timedelta

DEAD_GAP_MAX = 45          # idle gap (min) below this between commitments is "dead"
GRID = 15                  # start-time grid for flexible meetings (min)
WINDOW_START, WINDOW_END = "2027-10-11", "2027-10-15"   # hard limit
DETOUR, SPEED_KMH, PARK_MIN = 1.35, 20.0, 6
RUSH = (("08:00", "09:30"), ("16:30", "18:30"))          # San Francisco rush hours, local time
RUSH_FACTOR = 1.35
ASK_MIN_SAVED = 10         # rank_asks: a person worth < 10 min either way is not worth a phone call
_INF = float("inf")


# ---------------------------------------------------------------- helpers
def _mn(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _hm(x):
    return "%02d:%02d" % divmod(int(x), 60)


def _ampm(x):
    h, m = divmod(int(x), 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "am" if h % 24 < 12 else "pm")


def _dur(x):
    return "%dh%02d" % divmod(abs(int(x)), 60)


def _span(x):
    x = abs(int(x))
    return "%d min" % x if x < 60 else _dur(x)


def _check_date(d):
    if not (WINDOW_START <= d <= WINDOW_END):
        raise ValueError("date %s outside allowed week %s..%s" % (d, WINDOW_START, WINDOW_END))


def _place(m):
    """Place id of a meeting; None for remote meetings."""
    return None if m.get("kind") == "remote" else m.get("place")


def _pname(week, p):
    return week["places"][p]["name"].split(" · ")[0] if p in week["places"] else str(p)


# ---------------------------------------------------------------- travel
_RUSH_MIN = tuple((_mn(a), _mn(b)) for a, b in RUSH)


def is_rush(depart_min):
    return any(a <= depart_min < b for a, b in _RUSH_MIN)


def _km(week, a, b):
    pa, pb = week["places"][a], week["places"][b]
    la1, lo1, la2, lo2 = map(math.radians, (pa["lat"], pa["lng"], pb["lat"], pb["lng"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _drive(week, a, b):
    """Base (off-peak, before parking) drive minutes a -> b: OSRM matrix if it has the pair, else haversine."""
    v = (week.get("drive_min") or {}).get("%s|%s" % (a, b))
    return v if v is not None else _km(week, a, b) * DETOUR / SPEED_KMH * 60


def _leg(drive, factor):
    return int(math.ceil((drive * factor + PARK_MIN) / 5 - 1e-9) * 5)


def travel_min(week, a, b, depart_min=None):
    """Minutes to travel between place ids a and b (0 if same or either is None/remote).
    depart_min (minutes after midnight) applies the rush-hour factor; None = off-peak."""
    if a is None or b is None or a == b:
        return 0
    return _leg(_drive(week, a, b), RUSH_FACTOR if depart_min is not None and is_rush(depart_min) else 1.0)


def _travel_to(week, a, b, arrive_min):
    """Travel minutes for a leg that must arrive at arrive_min, leaving as late as possible (see module doc)."""
    return travel_min(week, a, b, depart_min=arrive_min - travel_min(week, a, b))


# ---------------------------------------------------------------- metrics
def metrics(week, date, sched):
    """Build the day timeline for sched = [{"id","start","place"}] (start "HH:MM")."""
    _check_date(date)
    by_id = {m["id"]: m for m in week["meetings"]}
    home = week["home"]
    items = []
    for s in sched:
        m = by_id[s["id"]]
        st = _mn(s["start"])
        pl = None if m.get("kind") == "remote" else s.get("place", m.get("place"))
        items.append((st, st + int(m["dur"]), s["id"], pl, m))
    items.sort(key=lambda x: (x[0], x[1], x[2]))

    segs, events, conflicts = [], [], []
    loc, prev_end, leave = home, None, None
    trav = dead = free = 0

    def seg(typ, a, b, **kw):
        d = {"type": typ, "start": _hm(a), "end": _hm(b), "start_min": a, "end_min": b, "min": b - a}
        d.update(kw)
        segs.append(d)

    for st, en, mid, pl, m in items:
        t = _travel_to(week, loc, pl, st)
        dep = st - t
        if prev_end is None:
            leave = dep
        else:
            gap = dep - prev_end
            if gap < 0:
                conflicts.append(mid)
            elif gap > 0:
                if gap < DEAD_GAP_MAX:
                    dead += gap
                    seg("dead", prev_end, dep, at=loc)
                else:
                    free += gap
                    seg("free", prev_end, dep, at=loc)
        if t > 0:
            trav += t
            seg("travel", dep, st, **{"from": loc, "to": pl, "rush": is_rush(dep)})
        seg("meeting", st, en, id=mid, place=pl)
        events.append({"id": mid, "title": m.get("title"), "person": m.get("person"),
                       "kind": m.get("kind"), "place": pl, "start": _hm(st), "end": _hm(en),
                       "start_min": st, "end_min": en, "dur": en - st})
        if pl is not None:
            loc = pl
        prev_end = en

    home_at = None
    if prev_end is not None:
        t = travel_min(week, loc, home, depart_min=prev_end)
        if t > 0:
            trav += t
            seg("travel", prev_end, prev_end + t, **{"from": loc, "to": home, "rush": is_rush(prev_end)})
        home_at = prev_end + t

    return {
        "date": date,
        "leave_home": None if leave is None else _hm(leave),
        "leave_home_min": leave,
        "home_at": None if home_at is None else _hm(home_at),
        "home_at_min": home_at,
        "travel_min": trav, "dead_min": dead, "free_min": free,
        "feasible": not conflicts, "conflicts": conflicts,
        "events": events, "segments": segs,
    }


# ---------------------------------------------------------------- search
def _cands(week, m, conf):
    """Candidate (start_min, place) list for a flexible meeting, and its original slot."""
    dur = int(m["dur"])
    orig = (_mn(m["start"]), _place(m))
    c = conf.get(m["id"]) or {}
    if m.get("kind") == "remote":
        places = [None]
    elif c.get("places"):
        places = list(dict.fromkeys(p for p in c["places"] if p in week["places"])) or [orig[1]]
    else:
        places = list(dict.fromkeys(p for p in [m.get("place")] + list(m.get("alt_places") or []) if p is not None))
    if "fixed_start" in c:
        return [(_mn(c["fixed_start"]), p) for p in places], orig
    starts = set()
    for w0, w1 in (c["windows"] if c.get("windows") else (m.get("flex") or [])):
        w0, w1 = _mn(w0), _mn(w1)
        s = -(-w0 // GRID) * GRID
        while s + dur <= w1:          # the whole meeting fits inside the window
            starts.add(s)
            s += GRID
    out = [(s, p) for s in sorted(starts) for p in places]
    if orig not in out:               # the original slot was agreed before, so it stays available
        out.append(orig)
    return out, orig


def _tables(week, places):
    """Off-peak (T0) and rush-hour (T1) travel minutes between every pair of places."""
    dr = {a: {b: _drive(week, a, b) for b in places if b != a} for a in places}
    T0 = {a: {b: 0 if a == b else _leg(dr[a][b], 1.0) for b in places} for a in places}
    T1 = {a: {b: 0 if a == b else _leg(dr[a][b], RUSH_FACTOR) for b in places} for a in places}
    return T0, T1


def _search(week, ms, confirmed, stats=None):
    """Engine "bnb": exact branch and bound (see module doc). -> {id: (start_min, place)} or None."""
    home = week["home"]
    fixed, flex = [], []
    for m in ms:
        if m.get("flexible"):
            cs, orig = _cands(week, m, confirmed)
            flex.append((m, cs, orig))
        else:
            st = _mn(m["start"])
            fixed.append((st, st + int(m["dur"]), _place(m), m["id"]))
    T0, T1 = _tables(week, {home} | {f[2] for f in fixed if f[2]} | {p for _, cs, _ in flex for _, p in cs if p})

    def compat(a, b):     # necessary condition for a and b on the same day (off-peak travel)
        if (a[0], a[1]) > (b[0], b[1]):
            a, b = b, a
        if a[1] > b[0]:
            return False
        return a[2] is None or b[2] is None or a[1] + T0[a[2]][b[2]] <= b[0]

    def hlb(it):          # lower bound on home_at if this candidate is in the plan
        return it[1] + (T0[it[2]][home] if it[2] is not None else 0)

    # rows: candidates (st, en, place) sorted by bound so the lowest set bit of a mask is its best bound
    rows = []
    for m, cs, orig in flex:
        dur = int(m["dur"])
        r = sorted(((s, s + dur, p) for s, p in cs), key=lambda it: (hlb(it), abs(it[0] - orig[0]), it[0], it[2] or ""))
        rows.append([it for it in r if all(compat(it, f) for f in fixed)])
    if any(not r for r in rows):
        return None
    order = sorted(range(len(flex)), key=lambda i: (len(rows[i]), flex[i][0]["id"]))   # fail first
    flex = [flex[i] for i in order]
    rows = [rows[i] for i in order]
    n = len(flex)
    hl = [[hlb(it) for it in row] for row in rows]
    orig = [(o[0], o[1]) for _, _, o in flex]
    pair = [[None] * n for _ in range(n)]     # pair[i][j][ci] -> mask over row j compatible with row i cand ci
    for i in range(n):
        for j in range(i + 1, n):
            pair[i][j] = [sum(1 << cj for cj, b in enumerate(rows[j]) if compat(a, b)) for a in rows[i]]
    fixed_t = tuple(f[:3] for f in fixed)
    fixed_lb = max((hlb(f) for f in fixed_t), default=0)
    ids = [f[0]["id"] for f in flex]

    def ev(seq):          # exact day cost, same rules as metrics()
        loc, prev, trav, dead = home, None, 0, 0
        for st, en, pl in seq:
            t = 0
            if pl is not None:
                t = T0[loc][pl]
                if t and is_rush(st - t):
                    t = T1[loc][pl]
            if prev is not None:
                gap = st - t - prev
                if gap < 0:
                    return None
                if gap < DEAD_GAP_MAX:
                    dead += gap
            trav += t
            if pl is not None:
                loc = pl
            prev = en
        if prev is None:
            return (0, 0)
        t = T1[loc][home] if is_rush(prev) else T0[loc][home]
        return (prev + t, trav + t + dead)

    best = [None, None]      # key, chosen candidates
    chosen = [None] * n
    st_ = stats if stats is not None else {}
    st_.setdefault("nodes", 0)
    st_.setdefault("leaves", 0)

    def rec(l, masks, lb):
        st_["nodes"] += 1
        if l == n:
            st_["leaves"] += 1
            r = ev(sorted(fixed_t + tuple(chosen), key=lambda x: (x[0], x[1])))
            if r is None:
                return
            moved = sum((chosen[i][0], chosen[i][2]) != orig[i] for i in range(n))
            shift = sum(abs(chosen[i][0] - orig[i][0]) for i in range(n))
            tie = tuple(sorted((ids[i], chosen[i][0], chosen[i][2] or "") for i in range(n)))   # == dp's
            key = (r[0], r[1], moved, shift, tie)
            if best[0] is None or key < best[0]:
                best[0], best[1] = key, list(chosen)
            return
        row, hrow, m = rows[l], hl[l], masks[l]
        while m:
            low = m & -m
            ci = low.bit_length() - 1
            m ^= low
            bh = best[0][0] if best[0] else _INF
            if hrow[ci] > bh:
                break                     # rows are sorted by bound: every later candidate is worse
            nlb = max(lb, hrow[ci])
            nm = list(masks)
            ok = True
            for j in range(l + 1, n):
                mj = masks[j] & pair[l][j][ci]
                if not mj:
                    ok = False
                    break
                nm[j] = mj
                b = hl[j][(mj & -mj).bit_length() - 1]
                if b > nlb:
                    nlb = b
            if not ok or nlb > bh:
                continue
            chosen[l] = row[ci]
            rec(l + 1, nm, nlb)
        chosen[l] = None

    rec(0, [(1 << len(r)) - 1 for r in rows], fixed_lb)
    if best[1] is None:
        return None
    out = {f[3]: (f[0], f[2]) for f in fixed}
    for (m, _, _), it in zip(flex, best[1]):
        out[m["id"]] = (it[0], it[2])
    return out


def _search_dp(week, ms, confirmed, stats=None):
    """Engine "dp": exact dynamic programme over the day in time order (same objective and output as "bnb").

    State (S, e, loc): S = bitmask of meetings already placed, e = end minute of the latest one, loc = where you
    are (the last in-person place; remote meetings do not move you). Everything after a state depends only on
    (S, e, loc), so per state we keep the lexicographically smallest partial cost (travel+dead, moves, minutes
    shifted, tie-break); all four parts add up along the day, so the principle of optimality holds.
    Transition: put any unplaced meeting i in a slot (s, s+dur, p) with s >= e and s - travel(loc->p, s) >= e;
    pay that travel plus the gap as dead time if it is under DEAD_GAP_MAX. Meetings may reorder around the
    fixed ones. A slot is only taken if every other unplaced meeting still has a slot starting at or after its
    end, so unplaced fixed meetings act as walls and dead branches are cut at once.
    Final states (S = all): home_at = e + travel(loc->home, departing e) is added, and the best
    (home_at, travel+dead, moves, shift, tie) wins. States are expanded in (e, |S|) order, so every
    predecessor is final before a state is expanded.
    Complexity: O(2^n * |E| * |L|) states x O(sum of slots) transitions, n = meetings that day, |E| = distinct
    end minutes (<= ~70 on the 15-min grid), |L| = places; the walls keep the reachable part far smaller.
    """
    import heapq
    home = week["home"]
    n = len(ms)
    if n == 0:
        return {}
    slots, orig, ids = [], [], []
    for m in ms:
        dur = int(m["dur"])
        if m.get("flexible"):
            cs, o = _cands(week, m, confirmed)
        else:
            o = (_mn(m["start"]), _place(m))
            cs = [o]
        cs = sorted(cs, key=lambda c: (c[0], c[1] or ""))
        slots.append([(c[0], c[0] + dur, c[1]) for c in cs])
        orig.append(o)
        ids.append(m["id"])
    T0, T1 = _tables(week, {home} | {p for row in slots for _, _, p in row if p})
    # tie-break = slot rank of each meeting, meetings in id order (most significant first), packed into one int
    weight, w = [0] * n, 1
    for i in sorted(range(n), key=lambda i: ids[i], reverse=True):
        weight[i] = w
        w *= len(slots[i])
    extra = [[(int((s, p) != orig[i]), abs(s - orig[i][0]), k * weight[i]) for k, (s, _, p) in enumerate(row)]
             for i, row in enumerate(slots)]
    starts = [[s for s, _, _ in row] for row in slots]
    last_start = [row[-1] for row in starts]
    full = (1 << n) - 1
    import bisect
    best = {(0, -1, home): ((0, 0, 0, 0), None, None)}
    heap = [(-1, 0, 0, home)]
    done = None
    nst = ntr = 0
    while heap:
        e, size, S, loc = heapq.heappop(heap)
        cost, _, _ = best[(S, e, loc)]
        nst += 1
        if S == full:
            t = T1[loc][home] if is_rush(e) else T0[loc][home]
            k = (e + t, cost[0] + t) + cost[1:]
            if done is None or k < done[0]:
                done = (k, (S, e, loc))
            continue
        todo = [i for i in range(n) if not S >> i & 1]
        for i in todo:
            wall = min((last_start[j] for j in todo if j != i), default=_INF)
            row, ex = slots[i], extra[i]
            for k in range(bisect.bisect_left(starts[i], e) if e >= 0 else 0, len(row)):
                s, en, p = row[k]
                if en > wall:
                    break
                t = 0
                if p is not None:
                    t = T0[loc][p]
                    if t and is_rush(s - t):
                        t = T1[loc][p]
                dead = 0
                if e >= 0:
                    gap = s - t - e
                    if gap < 0:
                        continue
                    if gap < DEAD_GAP_MAX:
                        dead = gap
                ntr += 1
                mv, sh, tie = ex[k]
                nc = (cost[0] + t + dead, cost[1] + mv, cost[2] + sh, cost[3] + tie)
                key = (S | 1 << i, en, p if p is not None else loc)
                old = best.get(key)
                if old is None:
                    heapq.heappush(heap, (en, size + 1, key[0], key[2]))
                if old is None or nc < old[0]:
                    best[key] = (nc, (S, e, loc), (i, k))
    if stats is not None:
        stats["states"] = stats.get("states", 0) + nst
        stats["transitions"] = stats.get("transitions", 0) + ntr
    if done is None:
        return None
    out, key = {}, done[1]
    while True:
        _, parent, pick = best[key]
        if pick is None:
            break
        i, k = pick
        out[ids[i]] = (slots[i][k][0], slots[i][k][2])
        key = parent
    return out


ENGINES = {"bnb": _search, "dp": _search_dp}
DEFAULT_ENGINE = "bnb"


def engine():
    """Search engine for this call: $HOMEBY4_ENGINE (read every call), default "bnb"."""
    import os
    name = (os.environ.get("HOMEBY4_ENGINE") or DEFAULT_ENGINE).strip().lower()
    if name not in ENGINES:
        raise ValueError("HOMEBY4_ENGINE=%r is not an engine; options: %s" % (name, ", ".join(sorted(ENGINES))))
    return ENGINES[name]


def _day(week, date):
    return [m for m in week["meetings"] if m["date"] == date]


def _plan(week, date, ms, confirmed, fallback_sched, search=_search, stats=None):
    """Best schedule (list of {id,start,place} in meeting order) and its metrics; fallback if infeasible."""
    best = search(week, ms, confirmed, stats)
    sched = fallback_sched if best is None else [
        {"id": m["id"], "start": _hm(best[m["id"]][0]), "place": best[m["id"]][1]} for m in ms]
    return sched, metrics(week, date, sched)


# ---------------------------------------------------------------- ask ranking
def _pin(m):
    p = _place(m)
    return {"fixed_start": m["start"], "places": [] if p is None else [p]}


def _rank(week, date, ms, confirmed, before_sched, plan, min_saved, search=_search):
    open_ = [m for m in ms if m.get("flexible") and m["id"] not in confirmed]
    pins = {m["id"]: _pin(m) for m in open_}
    nobody = dict(confirmed, **pins)     # every unasked person keeps their slot
    _, base = _plan(week, date, ms, nobody, before_sched, search)
    out = []
    for m in open_:
        yes = {k: v for k, v in nobody.items() if k != m["id"]}
        _, y = _plan(week, date, ms, yes, before_sched, search)
        _, no = _plan(week, date, ms, dict(confirmed, **{m["id"]: pins[m["id"]]}), before_sched, search)
        saved = max(0, (base["home_at_min"] or 0) - (y["home_at_min"] or 0))
        if_no = max(0, (no["home_at_min"] or 0) - (plan["home_at_min"] or 0))
        td = (base["travel_min"] + base["dead_min"]) - (y["travel_min"] + y["dead_min"])
        out.append({"id": m["id"], "person": m.get("person"), "saved_if_yes_min": saved, "if_no_min": if_no,
                    "travel_dead_saved_min": td, "skip": max(saved, if_no) < min_saved})
    out.sort(key=lambda r: (-r["saved_if_yes_min"], -r["if_no_min"], -r["travel_dead_saved_min"], r["id"]))
    return out


def rank_asks(week, date, confirmed=None, min_saved=ASK_MIN_SAVED):
    """Who to phone first. For every flexible meeting with no confirmed entry yet:
      saved_if_yes_min: home-at minutes gained if this one person agrees to anything in their flex window/places
                        while everyone else stays as currently confirmed (unasked people keep their slot);
      if_no_min:        home-at minutes the current plan loses if this person keeps their slot
                        (catches moves that only pay off together);
      travel_dead_saved_min: travel+dead minutes gained if this person agrees (tie-break);
      skip:             worth < min_saved minutes either way, so don't call.
    Ordered best call first. Re-run after each answer: values change as people confirm."""
    _check_date(date)
    confirmed = confirmed or {}
    ms = _day(week, date)
    before_sched = [{"id": m["id"], "start": m["start"], "place": _place(m)} for m in ms]
    search = engine()
    _, plan = _plan(week, date, ms, confirmed, before_sched, search)
    return _rank(week, date, ms, confirmed, before_sched, plan, min_saved, search)


# ---------------------------------------------------------------- explanations
def _clash(week, a, b):
    """True if slots a, b = (start, end, place) cannot both happen (overlap or travel does not fit)."""
    if (a[0], a[1]) > (b[0], b[1]):
        a, b = b, a
    return a[1] > b[0] or a[1] + _travel_to(week, a[2], b[2], b[0]) > b[0]


def _why(week, date, ms, before_sched, before, after_sched, after, mv, moves):
    by = {m["id"]: m for m in ms}
    mid = mv["id"]
    o = next(s for s in before_sched if s["id"] == mid)
    rev = metrics(week, date, [o if s["id"] == mid else s for s in after_sched])
    ctx, lead = [], ""
    if rev["feasible"]:          # effect of this move given the rest of the plan
        ref, cur = rev, after
    else:                        # its old slot collides with another move: it makes room
        dur = int(by[mid]["dur"])
        old = (_mn(o["start"]), _mn(o["start"]) + dur, o["place"])
        partner = where = None
        for other in moves:
            if other["id"] != mid:
                st = _mn(other["to_start"])
                if _clash(week, old, (st, st + int(by[other["id"]]["dur"]), other["to_place"])):
                    partner = other
                    where = _ampm(st) if other["to_place"] is None else "%s at %s" % (
                        _pname(week, other["to_place"]), _ampm(st))
                    break

        def applied(ids):        # before + only these moves (both scheds are in meeting order)
            return metrics(week, date, [a if a["id"] in ids else b for b, a in zip(before_sched, after_sched)])
        ref, cur = before, applied({mid})
        if cur["feasible"] and partner:
            ctx.append("makes room for %s at %s" % (partner["person"], where))
        elif partner:            # only works together with the move it makes room for
            cur = applied({mid, partner["id"]})
            lead = "together with moving %s to %s, " % (partner["person"], where)
        if not cur["feasible"]:
            cur = None
    eff = []
    if cur is not None:
        dh = (ref["home_at_min"] or 0) - (cur["home_at_min"] or 0)
        dt = ref["travel_min"] - cur["travel_min"]
        dd = ref["dead_min"] - cur["dead_min"]
        if dh > 0:
            eff.append("gets you home %s earlier" % _span(dh))
        if dt:
            eff.append("%s %d min driving" % ("saves" if dt > 0 else "adds", abs(dt)))
        if dd:
            eff.append("%s %d min of dead time" % ("removes" if dd > 0 else "adds", abs(dd)))
    if mv["to_place"] is not None:     # same spot as the meeting next to it: no drive in between
        evs = [e for e in after["events"] if e["place"] is not None]
        k = next(i for i, e in enumerate(evs) if e["id"] == mid)
        for j in (k + 1, k - 1):
            if 0 <= j < len(evs) and evs[j]["place"] == mv["to_place"]:
                ctx.append("%s is where you already are for %s at %s" % (
                    _pname(week, mv["to_place"]), evs[j]["person"], _ampm(evs[j]["start_min"])))
                break
    elif not ctx:
        ctx.append("it's a call, so it fits wherever you are")
    if len(eff) > 1:
        eff = [", ".join(eff[:-1]) + " and " + eff[-1]]
    if eff and lead:
        eff = [lead + eff[0]]
    s = "; ".join(eff + ctx) or "keeps the day feasible"
    return s[0].upper() + s[1:] + "."


# ---------------------------------------------------------------- solve
def solve(week, date, confirmed=None):
    _check_date(date)
    confirmed = confirmed or {}
    ms = _day(week, date)
    before_sched = [{"id": m["id"], "start": m["start"], "place": _place(m)} for m in ms]
    before = metrics(week, date, before_sched)
    search = engine()                       # $HOMEBY4_ENGINE, read at call time
    after_sched, after = _plan(week, date, ms, confirmed, before_sched, search)

    bmap = {s["id"]: s for s in before_sched}
    by = {m["id"]: m for m in ms}
    moves = []
    for s in after_sched:
        b = bmap[s["id"]]
        if (s["start"], s["place"]) != (b["start"], b["place"]):
            moves.append({"id": s["id"], "person": by[s["id"]].get("person"),
                          "from_start": b["start"], "to_start": s["start"],
                          "from_place": b["place"], "to_place": s["place"]})
    moves.sort(key=lambda mv: mv["to_start"])
    for mv in moves:
        mv["why"] = _why(week, date, ms, before_sched, before, after_sched, after, mv, moves)
    saved = (before["home_at_min"] - after["home_at_min"]) if before["home_at_min"] is not None else 0
    return {"date": date, "before": before, "after": after, "moves": moves,
            "saved_min": saved, "potential": any(mv["id"] not in confirmed for mv in moves),
            "ask_order": _rank(week, date, ms, confirmed, before_sched, after, ASK_MIN_SAVED, search)}


def solve_week(week, confirmed=None):
    d = _date.fromisoformat(week["window"]["start"])
    end = _date.fromisoformat(week["window"]["end"])
    out = {}
    while d <= end:
        out[d.isoformat()] = solve(week, d.isoformat(), confirmed)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------- CLI
def _describe(week, mt):
    lines = ["  leave %s  home %s  travel %dm  dead %dm  free %dm%s" % (
        mt["leave_home"], mt["home_at"], mt["travel_min"], mt["dead_min"], mt["free_min"],
        "" if mt["feasible"] else "  ** CONFLICTS: %s **" % ",".join(mt["conflicts"]))]
    titles = {e["id"]: e["title"] for e in mt["events"]}
    for s in mt["segments"]:
        if s["type"] == "meeting":
            what = "%s @ %s" % (titles[s["id"]], s["place"] or "remote")
        elif s["type"] == "travel":
            what = "%s -> %s%s" % (s["from"], s["to"], "  (rush hour)" if s.get("rush") else "")
        else:
            what = "at %s" % s["at"]
        lines.append("    %s-%s %-7s %s" % (s["start"], s["end"], s["type"].upper(), what))
    return "\n".join(lines)


def format_result(week, res):
    b, a = res["before"], res["after"]
    out = ["== %s ==" % res["date"], "BEFORE", _describe(week, b), "AFTER", _describe(week, a), "MOVES"]
    for mv in res["moves"] or []:
        out.append("  %s (%s): %s %s -> %s %s" % (mv["person"], mv["id"], mv["from_start"], mv["from_place"],
                                                   mv["to_start"], mv["to_place"]))
        out.append("      why: %s" % mv["why"])
    if not res["moves"]:
        out.append("  (none)")
    out.append("SAVED %d min (%s) home earlier; travel+dead %dm -> %dm; potential=%s" % (
        res["saved_min"], _dur(res["saved_min"]), b["travel_min"] + b["dead_min"],
        a["travel_min"] + a["dead_min"], res["potential"]))
    out.append("ASK ORDER")
    for r in res.get("ask_order") or []:
        out.append("  %-18s %-6s if yes -%3d min  if no +%3d min%s" % (
            r["person"], r["id"], r["saved_if_yes_min"], r["if_no_min"], "  (skip)" if r["skip"] else ""))
    return "\n".join(out)


def main(argv):
    if len(argv) < 2:
        print("usage: solver.py data/week.json [YYYY-MM-DD] [confirmed.json]")
        return 2
    with open(argv[1]) as f:
        week = json.load(f)
    import geo
    geo.localize(week)      # real home ($HOMEBY4_HOME) + OSRM matrix; offline it stays haversine. The functions above stay pure
    conf = None
    if len(argv) > 3:
        with open(argv[3]) as f:
            conf = json.load(f)
    if len(argv) > 2:
        print(format_result(week, solve(week, argv[2], conf)))
    else:
        for d, res in solve_week(week, conf).items():
            print("%s  home %s -> %s  saved %dm  moves %d  potential=%s  ask first: %s" % (
                d, res["before"]["home_at"], res["after"]["home_at"], res["saved_min"],
                len(res["moves"]), res["potential"],
                ", ".join(r["person"] for r in res["ask_order"] if not r["skip"]) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
