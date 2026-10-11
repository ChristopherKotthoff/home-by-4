# HomeBy4

Tell it when you want to be home. HomeBy4 finds which of your in-person meetings could move. It phones those people to agree a new time or place, then re-solves your day so the driving and the dead gaps disappear.

Built at SpeedHacks (a16z speedrun), 11 Oct 2026.

## Run
```bash
python3 server.py            # http://localhost:8787  (mock calls if DIAL_API_KEY is unset)
python3 test_solver.py       # solver checks
python3 dial.py              # allowlist + transcript-parsing self-check
python3 data/check_week.py   # demo-week hard-limit checks
```
No dependencies. It needs only Python 3.11 or later and its standard library.

**Optional live mode** (all variables are listed in `.env.example`; copy it to `.env`):
```bash
export DIAL_API_KEY=sk_live_...
export DIAL_NUMBER=+1...
export KEEWANO_TOKEN=...
export HOMEBY4_DEMO_PHONE=+1...   # the only number Dial may call (your own phone)
export HOMEBY4_HOME="..."         # your home address, geocoded once (OpenStreetMap)
python3 backfill.py
```
`backfill.py` loads 8 weeks of synthetic negotiation history into Keewano.

## How it works
1. **`solver.py`** (deterministic, exact). Driving times come from real road routing (OSRM over OpenStreetMap, see `geo.py`) between geocoded venues and your home, about 35% slower when a leg leaves at rush hour (08:00–09:30, 16:30–18:30). It counts any gap under 45 minutes as dead time. Flexible meetings are tried on a 15-minute grid within their windows and at each alternative place. It minimises, in order, the time you get home, then travel plus dead minutes, then the number of moves. Fixed meetings never move. It also ranks who to call first and gives each move a plain-English reason.
   - **Two exact engines**, chosen with `HOMEBY4_ENGINE`:
     - `bnb` (the default) is branch and bound with pairwise feasibility masks.
     - `dp` is a dynamic programme that walks the day in time order, tracking which meetings are placed, when the last one ends and where you are.
     - `test_solver.py` checks that both give the same answer as trying every option.
2. **`dial.py`.** Two outbound AI voice calls through Dial per move.
   - **Ask:** the agent says it's an AI, asks which times and places would work that day, and never commits. It says it will call back, and the server hangs up.
   - The transcript is turned into windows and acceptable places by a fast LLM (Azure OpenAI, `LLM_MODEL`, strict JSON schema, validated), with a regex fallback.
   - **Inform:** after the re-solve, a short second call tells the person the final slot.
3. **`server.py`.** Windows become constraints and the day is re-solved live. An agreed inform call pins the slot as confirmed.
4. **`calendar_sync.py`** (optional, macOS). With `HOMEBY4_CALENDAR_SYNC=1` the solved week is written into a real Calendar.app calendar (`HOMEBY4_CALENDAR`, only the demo week's events). It saves a snapshot before the first write. To put the week back, use `python3 calendar_sync.py reset` or the "Put my calendar back" button.
5. **`keewano.py`.** Every outcome is logged as `call_outcome` and `day_compacted` events, so you can ask Keewano things like "who never agrees to move?"

## Which meetings get a call
Not every flexible meeting should be moved. Before the solver plans, `triage.py` asks an LLM (Azure OpenAI, `LLM_MODEL`) to judge every flexible entry. It sees the attendee count, the person's role, the meeting note and how many minutes earlier the owner would get home if that meeting moved. It says no to board meetings, an investor who may lead the round, customer-critical meetings, medical appointments and anything with four or more attendees. It also skips calls that would gain less than 30 minutes. A meeting that is not worth a call is treated as fixed: the solver plans around it and nobody phones that person.

- **Cached verdicts.** The answers live in `data/triage.json` and are reused until a meeting changes. `python3 triage.py` prints the table. `python3 triage.py --refresh` asks the LLM again. Without LLM credentials a simple rule applies (four or more attendees, or board, demo, customer or check-up in the title). That fallback is never cached.
- **Optional email context.** Set `HOMEBY4_EMAIL_MCP` to the command that starts an email MCP server over stdio, for example `export HOMEBY4_EMAIL_MCP="npx -y @gongrzhe/server-gmail-autoauth-mcp"`, then run `python3 triage.py --refresh`. For each flexible meeting triage searches the last 30 days for the person's name and the meeting type (5 mails at most, 300 characters each) and gives the snippets to the LLM as untrusted data. The code can call only `search_emails` and `read_email`, so it cannot send, draft or change mail. If the server is missing, slow (15 s limit) or has no `search_emails`, triage carries on without context. Snippets that were used are stored in `data/triage.json`; delete the file to drop them.
- **MCP.** `triage_week` returns the table and `explain_meeting {meeting_id}` gives the reason and any email snippets.

## Connect your agent (MCP)
`mcp_server.py` lets any MCP client (Claude Code, Claude Desktop, others) look at the week, ask people for availability and confirm the new times. It is a thin proxy over the HTTP API, so the server must already be running (`python3 server.py`). All safety limits stay in `server.py`. Set `HOMEBY4_URL` if the server is not on `http://localhost:8787`.

Claude Code:
```bash
claude mcp add homeby4 -- python3 "$(pwd)/mcp_server.py"
```

Claude Desktop, in `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "homeby4": {
      "command": "python3",
      "args": ["/path/to/home-by-4/mcp_server.py"],
      "env": { "HOMEBY4_URL": "http://localhost:8787" }
    }
  }
}
```

Tools: `get_week`, `get_day`, `triage_week`, `explain_meeting`, `ask_availability`, `confirm_new_time`, `get_call`, `reset_demo`. `ask_availability` and `confirm_new_time` place a real phone call (to the allowlisted number), and only one call can run at a time. Poll `get_call` every few seconds until it is `ended` or `failed`.

Example prompt: "Get me home earlier on Tuesday: ask whoever needs to move, then confirm."

## Time saved
- **Whose time:** yours. You get home earlier, with less driving and fewer unusable gaps.
- **How it's measured:** the solver computes every number from the calendar, real driving times and the real sunset at your home.
- **What counts:** only moves a person confirmed on the callback. Moves not yet confirmed are shown as potential.
- **Demo week:** on Tuesday you get home at 4:15 pm instead of 6:25 pm, 2 h 10 min earlier. Across the week it is about 5 h 20 min if everyone triage picks agrees (Mon 0, Tue 130, Wed 60, Thu 70, Fri 60 min).

## Safety limits, enforced in code
- **One week:** the demo week is hard-limited to **2027-10-11 to 2027-10-15**. `server.py`, `solver.py`, `dial.py`, `calendar_sync.py` and `make_ics.py` refuse anything outside it.
- **One number:** Dial may call only the number in `HOMEBY4_DEMO_PHONE`, set in `.env` and never committed. `data/week.json` uses the placeholder +1 555-0100, and every meeting is routed to that one number.
- **Your home stays local:** `HOMEBY4_HOME` is geocoded once and cached in `data/home.json`, which is gitignored.
- **No invitations:** the people in the demo week are fictional, and `data/demo_week.ics` has no attendees, so importing it emails nobody.

## Partner tools
| Tool | What it does here |
|---|---|
| **Dial** | Outbound AI voice calls: ask for availability, then confirm the solved slot |
| **Keewano** | Negotiation memory, plus live natural-language questions over it |
| **Alike** | Build-time coordination between parallel coding-agent sessions: decisions such as "one phone number only" recorded against the files they govern |

