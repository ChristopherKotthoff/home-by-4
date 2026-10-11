#!/usr/bin/env python3
"""HomeBy4 MCP server (stdio). Thin proxy over the running HomeBy4 HTTP server.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout. Logs go to stderr only.
Env: HOMEBY4_URL (default http://localhost:8787). Python stdlib only.

All safety guards (demo-week date limit, single allowlisted phone number, one call at a
time) live in server.py / dial.py. This file only forwards requests and shrinks replies.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("HOMEBY4_URL", "http://localhost:8787").rstrip("/")
DEFAULT_PROTOCOL = "2025-06-18"
HTTP_TIMEOUT = 40  # GET /api/call can wait up to ~20 s for Dial's transcript


def log(*a):
    print("[homeby4-mcp]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ HTTP

class ToolError(Exception):
    pass


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or e.reason
        except Exception:  # noqa: BLE001
            msg = e.reason
        raise ToolError("HomeBy4 server refused the request (HTTP %s): %s" % (e.code, msg))
    except (urllib.error.URLError, OSError) as e:
        raise ToolError("Cannot reach the HomeBy4 server at %s (%s). Start it with: python3 server.py" %
                        (BASE, getattr(e, "reason", e)))
    try:
        return json.loads(raw)
    except ValueError:
        raise ToolError("HomeBy4 server returned a non-JSON reply")


# ------------------------------------------------------------------ shaping helpers

def weekday(date):
    import datetime
    return datetime.date.fromisoformat(date).strftime("%A")


def pname(week, pid):
    if not pid:
        return "remote"
    n = (week.get("places") or {}).get(pid, {}).get("name") or pid
    return n.split(" · ")[0]


def home_at(plan):
    return plan.get("home_at")


def real_moves(day):
    return [m for m in day.get("moves", [])
            if m.get("to_start") != m.get("from_start") or m.get("to_place") != m.get("from_place")]


def timeline(week, plan):
    """One short string per segment, in plain terms."""
    ev = {e["id"]: e for e in plan.get("events", [])}
    out = []
    for s in plan.get("segments", []):
        span = "%s-%s" % (s["start"], s["end"])
        t = s["type"]
        if t == "meeting":
            e = ev.get(s.get("id"), {})
            out.append("%s meeting %s: %s @ %s" % (span, s.get("id"), e.get("title", "?"), pname(week, s.get("place"))))
        elif t == "travel":
            out.append("%s travel %d min %s -> %s" % (span, s["min"], pname(week, s.get("from")), pname(week, s.get("to"))))
        elif t == "dead":
            out.append("%s dead gap %d min (too short to use) at %s" % (span, s["min"], pname(week, s.get("at"))))
        elif t == "free":
            out.append("%s free %d min at %s" % (span, s["min"], pname(week, s.get("at"))))
        else:
            out.append("%s %s" % (span, t))
    out.append("home at %s" % plan.get("home_at"))
    return out


def plan_summary(week, plan):
    return {"leave_home": plan.get("leave_home"), "home_at": plan.get("home_at"),
            "travel_min": plan.get("travel_min"), "dead_min": plan.get("dead_min"),
            "timeline": timeline(week, plan)}


def saved(day):
    if day.get("saved_min") is not None:
        return day["saved_min"]
    return (day["before"].get("home_at_min") or 0) - (day["after"].get("home_at_min") or 0)


# ------------------------------------------------------------------ tools

def t_get_week(args):
    st = http("GET", "/api/state")
    days = []
    for date, d in sorted(st.get("days", {}).items()):
        days.append({"date": date, "weekday": weekday(date), "home_before": home_at(d["before"]),
                     "home_after_potential": home_at(d["after"]), "minutes_saved": saved(d),
                     "proposed_moves": len(real_moves(d)), "needs_confirmation": bool(d.get("potential"))})
    return {"owner": st.get("week", {}).get("owner"), "calls_mode": st.get("mode"),
            "total_minutes_saved": sum(x["minutes_saved"] for x in days), "days": days}


def t_get_day(args):
    date = args.get("date")
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ToolError("date must be a string like 2027-10-12")
    st = http("GET", "/api/state")
    week, days = st.get("week", {}), st.get("days", {})
    d = days.get(date)
    if not d:
        raise ToolError("no plan for %s. Available dates: %s" % (date, ", ".join(sorted(days))))
    flex = {m["id"]: m for m in week.get("meetings", [])}
    moves, real = [], real_moves(d)
    for m in d.get("moves", []):
        moves.append({"meeting_id": m["id"], "person": m.get("person"),
                      "from": "%s @ %s" % (m.get("from_start"), pname(week, m.get("from_place"))),
                      "to": "%s @ %s" % (m.get("to_start"), pname(week, m.get("to_place"))),
                      "stage": m.get("stage"), "chosen_start": m.get("chosen_start"),
                      "chosen_place": pname(week, m.get("chosen_place")) if m.get("chosen_place") else None,
                      "moves": m in real, "flexible": bool(flex.get(m["id"], {}).get("flexible"))})
    return {"date": date, "weekday": weekday(date), "minutes_saved": saved(d),
            "needs_confirmation": bool(d.get("potential")),
            "before": plan_summary(week, d["before"]), "after": plan_summary(week, d["after"]),
            "moves": moves,
            "stages": "proposed = nobody asked yet; asked = they gave windows, solver re-planned; confirmed = they agreed to the final slot"}


def _meeting_id(args):
    mid = args.get("meeting_id")
    if not isinstance(mid, str) or not mid.strip():
        raise ToolError("meeting_id is required, e.g. 'tue-5' (see 'moves' in get_day)")
    return mid.strip()


def _call_summary(c):
    c = c["call"] if isinstance(c.get("call"), dict) else c
    keys = ("meeting_id", "person", "kind", "status", "result", "outcome", "error",
            "from_start", "from_place", "to_start", "to_place", "mode")
    out = {"call_id": c.get("id")}
    out.update({k: c[k] for k in keys if c.get(k) is not None})
    out["transcript"] = [{"speaker": t.get("speaker"), "text": t.get("text")}
                         for t in (c.get("turns") or []) if isinstance(t, dict)]
    if c.get("status") in ("queued", "in_progress", "parsing"):
        out["next"] = "Call not finished. Poll get_call again in about 5 seconds."
    return out


def t_place_call(kind):
    def run(args):
        c = http("POST", "/api/call", {"meeting_id": _meeting_id(args), "kind": kind})
        out = _call_summary(c)
        out["next"] = "Call placed. Poll get_call with this call_id every ~5 s until status is ended or failed."
        return out
    return run


def t_get_call(args):
    cid = args.get("call_id")
    if not isinstance(cid, str) or not re.fullmatch(r"[\w.\-]+", cid):
        raise ToolError("call_id is required (returned by ask_availability / confirm_new_time)")
    c = http("GET", "/api/call/" + cid)
    out = _call_summary(c)
    day = c.get("day")
    if c.get("status") == "ended" and isinstance(day, dict) and day.get("after"):
        out["day_after"] = {"date": day.get("date") or day["after"].get("date"), "home_at_before": home_at(day["before"]),
                            "home_at_after": home_at(day["after"]), "minutes_saved": saved(day)}
    if c.get("status") == "ended" and out.get("outcome") is None:
        out["note"] = "Nothing usable was parsed from the call; confirmed state is unchanged."
    return out


def t_reset(args):
    http("POST", "/api/reset", {})
    return {"ok": True, "note": "All calls and confirmations cleared. Every move is back to 'proposed'."}


def t_triage_week(args):
    import triage  # imported directly (no HTTP): reads data/triage.json, or runs the LLM once if it is missing
    week = triage.load_week()
    verdicts = triage.load_or_run(week)
    rows = [{"meeting_id": m["id"], "date": m["date"], "weekday": weekday(m["date"]), "person": m["person"],
             "title": m["title"], **verdicts[m["id"]]} for m in week["meetings"] if m.get("flexible")]
    return {"worth_calling": sum(r["worth_calling"] for r in rows), "flexible_meetings": len(rows),
            "meetings": rows,
            "note": "worth_calling false = treated as fixed by the planner; use explain_meeting for details"}


def t_explain_meeting(args):
    import triage
    try:
        return triage.explain(triage.load_week(), _meeting_id(args))
    except StopIteration:
        raise ToolError("unknown meeting_id %r (see triage_week for ids)" % args.get("meeting_id"))


TOOLS = [
    {"name": "get_week",
     "description": ("Overview of the demo week (Mon-Fri) for the calendar owner. For each day: when they get home today "
                     "(home_before), when they could get home if the solver's proposed moves happen (home_after_potential), "
                     "minutes_saved, and how many in-person meetings are proposed to move. Start here, then call get_day "
                     "on the day with the most minutes_saved. Read-only; places no calls."),
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "fn": t_get_week},
    {"name": "get_day",
     "description": ("Before/after timeline for one day in plain terms: meetings with times and places, travel minutes, "
                     "dead gaps (too short to use), free blocks, and the time the owner gets home. Also lists the moves "
                     "(meetings to shift) with meeting_id, person, from/to, stage (proposed = nobody asked yet, "
                     "asked = they gave availability windows, confirmed = they agreed to the final slot), and "
                     "chosen_start/chosen_place (the solver's current plan for that meeting). Use the meeting_id values "
                     "with ask_availability and confirm_new_time. Read-only; places no calls."),
     "inputSchema": {"type": "object", "properties": {"date": {"type": "string", "description": "Day as YYYY-MM-DD, e.g. 2027-10-12 (Tuesday). Only the demo week 2027-10-11 to 2027-10-15 exists."}},
                     "required": ["date"], "additionalProperties": False},
     "fn": t_get_day},
    {"name": "triage_week",
     "description": ("Which flexible meetings are worth phoning? An LLM judged every flexible calendar entry (attendees, "
                     "importance such as board, investor or customer, how much earlier the owner would get home, and "
                     "optionally recent emails). Returns one row per flexible meeting: worth_calling, movable "
                     "(yes/maybe/no), a short reason and the source (llm or rule). Meetings with worth_calling false "
                     "are treated as fixed, so do not phone them. Read-only; places no calls."),
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "fn": t_triage_week},
    {"name": "explain_meeting",
     "description": ("Why a meeting is or is not worth a call: the triage verdict, the reason, and any email snippets "
                     "that were used as context. Read-only; places no calls."),
     "inputSchema": {"type": "object", "properties": {"meeting_id": {"type": "string", "description": "Meeting id, e.g. 'wed-3' (see triage_week)."}},
                     "required": ["meeting_id"], "additionalProperties": False},
     "fn": t_explain_meeting},
    {"name": "ask_availability",
     "description": ("PLACES A REAL PHONE CALL. An AI voice agent phones the person for this meeting, says it is an AI "
                     "calling for the calendar owner, and asks which time ranges and places would work that day. It "
                     "commits to nothing. Afterwards the solver re-plans the day around their answers (stage becomes "
                     "'asked'). Only ONE call can run at a time: if another is queued or in progress the server answers "
                     "with a 409 error, so wait for get_call to show ended or failed first. The meeting must be flexible "
                     "(a move in get_day). Returns a call_id: poll get_call every ~5 s. Typical flow: ask_availability "
                     "for each person whose meeting needs to move, one after another, then confirm_new_time for each."),
     "inputSchema": {"type": "object", "properties": {"meeting_id": {"type": "string", "description": "Meeting id from get_day moves, e.g. 'tue-5'."}},
                     "required": ["meeting_id"], "additionalProperties": False},
     "fn": t_place_call("ask")},
    {"name": "confirm_new_time",
     "description": ("PLACES A REAL PHONE CALL. Calls the person back and tells them the solver's chosen new time and "
                     "place for this meeting (chosen_start/chosen_place in get_day, frozen when the call is placed), "
                     "and asks if that works. If they agree the move becomes 'confirmed' and counts toward time saved. "
                     "Use after ask_availability for the same meeting has ended. Only ONE call can run at a time (409 "
                     "otherwise). Returns a call_id: poll get_call every ~5 s."),
     "inputSchema": {"type": "object", "properties": {"meeting_id": {"type": "string", "description": "Meeting id of a meeting that was already asked, e.g. 'tue-5'."}},
                     "required": ["meeting_id"], "additionalProperties": False},
     "fn": t_place_call("inform")},
    {"name": "get_call",
     "description": ("Status and result of a call placed by ask_availability or confirm_new_time. status is one of "
                     "queued, in_progress, parsing, ended, failed. Poll about every 5 seconds until status is 'ended' or "
                     "'failed'. Returns the transcript (speaker/text turns), the parsed outcome (for ask: windows and "
                     "acceptable places or declined; for inform: agreed true/false), a result label (windows, declined, "
                     "agreed, not_agreed, unparsed) and, once ended, the day's new home time. Read-only."),
     "inputSchema": {"type": "object", "properties": {"call_id": {"type": "string", "description": "call_id returned by ask_availability or confirm_new_time."}},
                     "required": ["call_id"], "additionalProperties": False},
     "fn": t_get_call},
    {"name": "reset_demo",
     "description": ("DESTRUCTIVE: clears all calls and all confirmed/asked results on the server, so every move goes back "
                     "to 'proposed'. Does not place a call. Only use when the user explicitly asks to start over."),
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "fn": t_reset},
]
BY_NAME = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------------ JSON-RPC

def reply(msg_id, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        m["error"] = error
    else:
        m["result"] = result
    sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()


def handle(msg):
    if not isinstance(msg, dict):
        return reply(None, error={"code": -32600, "message": "Invalid Request"})
    method, mid = msg.get("method"), msg.get("id")
    is_request = "id" in msg and method is not None
    params = msg.get("params") or {}
    if not is_request:
        return  # notifications (e.g. notifications/initialized) and stray responses: no reply
    if method == "initialize":
        return reply(mid, {"protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "homeby4", "version": "0.1.0"}})
    if method == "ping":
        return reply(mid, {})
    if method == "tools/list":
        return reply(mid, {"tools": [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]})
    if method == "tools/call":
        tool = BY_NAME.get(params.get("name"))
        if not tool:
            return reply(mid, error={"code": -32602, "message": "Unknown tool: %s" % params.get("name")})
        args = params.get("arguments") or {}
        try:
            if not isinstance(args, dict):
                raise ToolError("arguments must be an object")
            text, is_error = json.dumps(tool["fn"](args), separators=(",", ":"), ensure_ascii=False), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # noqa: BLE001
            log("tool crashed:", repr(e))
            text, is_error = "Internal error in %s: %s: %s" % (params.get("name"), type(e).__name__, e), True
        return reply(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return reply(mid, error={"code": -32601, "message": "Method not found: %s" % method})


def main():
    log("ready, proxying to", BASE)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        try:
            for m in (msg if isinstance(msg, list) else [msg]):
                handle(m)
        except Exception as e:  # noqa: BLE001
            log("handler crashed:", repr(e))
            if isinstance(msg, dict) and "id" in msg:
                reply(msg["id"], error={"code": -32603, "message": "Internal error: %s" % e})


if __name__ == "__main__":
    main()
