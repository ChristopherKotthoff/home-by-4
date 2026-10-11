"""HomeBy4 real geography. Python stdlib only; every network call has a short timeout and returns None when offline.

geocode(query, cache=)      -> (lat, lng, display_name) | None   OpenStreetMap Nominatim, 1 request/s, JSON-cached
drive_matrix(points, cache=) -> {(a, b): minutes} | None          one OSRM /table request for all pairs, JSON-cached
sunset(lat, lng, date, tz)  -> "HH:MM"                            NOAA solar algorithm (accurate to about a minute)
localize(week)              -> {"home": "env"|"placeholder", "travel": "osrm"|"haversine"}
    makes a loaded week real, in place: home from $HOMEBY4_HOME (never from the repo; the address and its coordinates
    are never printed), week["drive_min"] = {"a|b": minutes}. solver.travel_min uses the matrix when the pair is in it.
Env: HOMEBY4_HOME (address), HOMEBY4_HOME_LABEL (UI name, default "Home"), HOMEBY4_OFFLINE=1 (skip all network calls).
"""
import hashlib
import json
import math
import os
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date as _date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
GEO_CACHE = os.path.join(ROOT, "data", "geocode_cache.json")      # public venues only (gitignored anyway)
HOME_CACHE = os.path.join(ROOT, "data", "home.json")              # the user's home (gitignored)
TRAVEL_CACHE = os.path.join(ROOT, "data", "travel_cache.json")    # OSRM matrix for one set of coordinates (gitignored)
UA = "homeby4-hackathon/0.1"
TIMEOUT = 8
_last = [0.0]


def _ctx():
    """Verified TLS; python.org builds of Python ship no CA bundle, so fall back to the system one."""
    c = ssl.create_default_context()
    if not c.cert_store_stats()["x509_ca"] and os.path.exists("/etc/ssl/cert.pem"):
        c.load_verify_locations("/etc/ssl/cert.pem")
    return c


def _get(url):
    if os.environ.get("HOMEBY4_OFFLINE") == "1":
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx()) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 - offline, DNS, 429, bad JSON: the caller falls back
        return None


def _load(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, d):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def geocode(query, cache=GEO_CACHE):
    """(lat, lng, display_name) for a free-text place, or None (offline / no hit). Cached by a hash of the query."""
    key = hashlib.sha1(query.strip().lower().encode()).hexdigest()[:16]
    c = _load(cache)
    if key in c:
        return tuple(c[key])
    time.sleep(max(0.0, _last[0] + 1.1 - time.time()))             # Nominatim policy: max 1 request per second
    _last[0] = time.time()
    r = _get("https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(query))
    if not r:
        return None
    out = (round(float(r[0]["lat"]), 6), round(float(r[0]["lon"]), 6), r[0].get("display_name", ""))
    c[key] = out
    _save(cache, c)
    return out


def drive_matrix(points, cache=TRAVEL_CACHE):
    """points = {id: (lat, lng)} -> {(a, b): driving minutes, free flow} for a != b, or None (offline).
    The cache holds the last matrix, keyed by the coordinates, so moving any point (e.g. home) invalidates it."""
    ids = list(points)
    key = hashlib.sha1(json.dumps([[i, round(points[i][0], 5), round(points[i][1], 5)] for i in ids]).encode()).hexdigest()[:16]
    c = _load(cache)
    if c.get("key") == key and isinstance(c.get("min"), dict):
        return {tuple(k.split("|")): v for k, v in c["min"].items()}
    r = _get("https://router.project-osrm.org/table/v1/driving/%s?annotations=duration"
             % ";".join("%.6f,%.6f" % (points[i][1], points[i][0]) for i in ids))
    if not r or r.get("code") != "Ok":
        return None
    out = {(a, b): round(r["durations"][i][j] / 60, 2) for i, a in enumerate(ids) for j, b in enumerate(ids)
           if a != b and r["durations"][i][j] is not None}
    _save(cache, {"key": key, "min": {"%s|%s" % k: v for k, v in out.items()}})
    return out


def sunset(lat, lng, day, tz="America/Los_Angeles"):
    """Local sunset "HH:MM" on ISO date `day` (NOAA general solar position formulas, refracted horizon 90.833 deg)."""
    from zoneinfo import ZoneInfo
    n = _date.fromisoformat(day).toordinal() - _date(2000, 1, 1).toordinal()
    rad = math.radians

    def sun(t_min):                       # (declination rad, equation of time min) at t_min UTC of the day
        T = (2451544.5 + n + t_min / 1440 - 2451545.0) / 36525
        L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360
        M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
        e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
        C = (math.sin(rad(M)) * (1.914602 - T * (0.004817 + 0.000014 * T)) + math.sin(rad(2 * M)) * (0.019993 - 0.000101 * T)
             + math.sin(rad(3 * M)) * 0.000289)
        om = 125.04 - 1934.136 * T
        lam = L0 + C - 0.00569 - 0.00478 * math.sin(rad(om))
        eps = 23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60 + 0.00256 * math.cos(rad(om))
        y = math.tan(rad(eps) / 2) ** 2
        eq = 4 * math.degrees(y * math.sin(2 * rad(L0)) - 2 * e * math.sin(rad(M))
                              + 4 * e * y * math.sin(rad(M)) * math.cos(2 * rad(L0)) - 0.5 * y * y * math.sin(4 * rad(L0))
                              - 1.25 * e * e * math.sin(2 * rad(M)))
        return math.asin(math.sin(rad(eps)) * math.sin(rad(lam))), eq

    t = 1080.0 - lng * 4                  # first guess: 18:00 local solar time, refined twice at the new estimate
    for _ in range(3):
        decl, eq = sun(t)
        cos_h = math.cos(rad(90.833)) / (math.cos(rad(lat)) * math.cos(decl)) - math.tan(rad(lat)) * math.tan(decl)
        t = 720 - 4 * (lng - math.degrees(math.acos(max(-1.0, min(1.0, cos_h))))) - eq
    utc = datetime(*_date.fromisoformat(day).timetuple()[:3], tzinfo=timezone.utc) + timedelta(minutes=t)
    return utc.astimezone(ZoneInfo(tz)).replace(second=0, microsecond=0).strftime("%H:%M")


def localize(week):
    """Make the week's geography real, in place (see module doc). Logs only home=env|placeholder, never the address."""
    st = {"home": "placeholder", "travel": "haversine"}
    addr = os.environ.get("HOMEBY4_HOME", "").strip()
    p = week["places"][week["home"]]
    g = geocode(addr, cache=HOME_CACHE) if addr else None
    if g:
        p["lat"], p["lng"], p["name"] = g[0], g[1], os.environ.get("HOMEBY4_HOME_LABEL") or "Home"
        st["home"] = "env"
    m = drive_matrix({k: (v["lat"], v["lng"]) for k, v in week["places"].items()})
    if m:
        week["drive_min"] = {"%s|%s" % k: v for k, v in m.items()}
        st["travel"] = "osrm"
    print("[geo] home=%s travel=%s%s" % (st["home"], st["travel"], "" if addr else "  (set HOMEBY4_HOME to use your real home)"),
          flush=True)
    return st
