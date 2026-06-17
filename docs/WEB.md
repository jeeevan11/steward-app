# Web console (local tuning cockpit)

A second, **read + approve** front-end at http://localhost:5173 over the *same* core
as Telegram. It does not replace Telegram and it reimplements nothing — every write
calls the exact guarded functions the Telegram bot calls. Localhost only.

## How it's wired

```
browser :5173  ──/api──▶  Vite dev server  ──proxy──▶  FastAPI :8000 (127.0.0.1)
                                                          │ reads:  storage/read_queries.py (read-only)
                                                          │ writes: assistant/web/service.py → existing guarded seams
                                                          ▼
                                              same SQLite DB (WAL)  ◀── the running assistant (run.py)
```

Two processes, one DB. **Safe by construction:** every write guard is a SQLite
compare-and-set, not in-memory state — so a console click and a Telegram tap on the
same item can't both win. The GUI cannot bypass a guard because the guard lives in
the database.

## Running it

Three terminals (the assistant must be running to actually process mail):

```
# 1) the assistant itself (processing + Telegram) — you already run this
python run.py

# 2) the console backend (FastAPI, 127.0.0.1:8000)
python run_web.py            # or: python -m assistant.web.api

# 3) the console UI (Vite, 127.0.0.1:5173)  — needs Node/npm
cd assistant/web/frontend
npm install
npm run dev
# open http://localhost:5173
```

Dry-run vs live is read from `.env` (`MODE`) exactly like the rest of the system.
In dry-run the console shows what *would* happen and changes nothing.

## Endpoints → which existing function each one calls (the safety audit trail)

### Reads (read-only, `storage/read_queries.py` + `storage/decision_log.py`)
| Endpoint | Reads |
|---|---|
| `GET /api/status` | settings + `repositories.is_paused` |
| `GET /api/stats` | `decision_log.stats` + `repositories.open_pending` |
| `GET /api/pipeline` | `decision_log.recent` + ledger PROCESSING count |
| `GET /api/queue` | `decision_log.recent` + latest `pending_actions` per message |
| `GET /api/email/{id}` | `decision_log.get` + latest `pending_actions` |
| `GET /api/contacts` | `contacts` table |
| `GET /api/rules` | `repositories.list_rules` |
| `GET /api/audit` | `repositories.recent_actions` |

### Writes (every one goes through the EXACT Telegram seam)
| Console button | Endpoint | Calls (identical to Telegram) |
|---|---|---|
| **Send this** | `POST /api/actions/{id}/approve` | `repositories.mark_approved` → `action.gmail_actions.execute_send` (which uses `repositories.begin_send`, dry-run aware) → `learning.recorder.record_approve` |
| **Edit first → Save** | `POST /api/actions/{id}/edit` | `repositories.set_pending_draft` (guard) → `learning.recorder.record_edit` |
| **Skip** | `POST /api/actions/{id}/skip` | `repositories.mark_skipped` (guard) → `learning.recorder.record_skip` → `learning.updater.maybe_propose_rule` |
| **Was this the right call?** | `POST /api/email/{id}/feedback` | `learning.recorder.record_override` (+ `learning.updater.maybe_propose_rule` when over-surfaced) |
| **Test the brain** | `POST /api/eval` | real `brain.classifier` + `brain.tiers` on an **in-memory DB, dry-run forced** — no DB writes, no Gmail, no send |

These are the same `repositories`/`gmail_actions`/`recorder`/`updater` functions in
`assistant/control/telegram_bot.py` (`_handle_approve`, `_handle_skip`, the edit path).
The double-send / no-revive guards (`begin_send`, `mark_approved`, `set_pending_draft`)
therefore protect the console identically — including across the two processes.

## What the core change was
One additive line in `main.process_one` calls `storage.decision_log.record(...)` to
persist each decision (sender/subject/snippet + reasoning/stakes/reversibility +
base-vs-final tier). It's best-effort (never raises) and changes no behavior — it's
what powers the detail pane's "why" and the "nearly filed but looked important" stat.
Nothing in the core imports `assistant/web/`.

## Tests
- `tests/test_web_read_queries.py` — read aggregations + plain-English mapping (stdlib only).
- `tests/test_web_endpoints.py` — FastAPI TestClient (skipped if FastAPI absent). Includes
  the required guard test: a console-initiated approve on an already-SENT action returns
  "already handled" and does **not** double-send. Also covers the P6 endpoints and that
  `POST /api/test-pipeline` has **zero side effects**.

---

# P6 — the rebuilt dashboard (views + endpoints)

The console is a single-page app (sidebar tabs) over the same backend. New in P6:

## Views
1. **Live queue** — the queue + a pipeline strip (Gmail → Normalize → THINK → JUDGE →
   CRITIQUE → Guardrails → Action → Telegram), updated over a **WebSocket** (`/ws/pipeline`,
   3s poll fallback).
2. **Detail** — original message, the AI's read, **How it decided** (collapsible
   THINK/JUDGE/CRITIQUE + quality gate), the draft, and Approve/Edit/Skip + feedback.
3. **Commitments** — open promises + stale threads, with Done / Snooze / (Draft via Telegram).
4. **Contacts** — searchable, inline-editable importance + VIP flag.
5. **Voice** — the four segment profiles (sample counts, tone summary, examples) + Rebuild.
6. **Rules** — active rules + **proposed** rules with Confirm / Reject.
7. **Audit log** — filterable; CSV export.
8. **Metrics** — tier-volume + handled-vs-surfaced bars, approval/edit rates, **response-time
   p50/p95**, and LLM cost by task (from the nightly pre-aggregated snapshot).
9. **Test the brain** — paste an email, run the FULL pipeline (all steps), zero side effects.
10. **WhatsApp** — relay health.

## New endpoints (all reads cached 30s / queue 5s; writes invalidate)
`GET /api/pipeline/status`, `GET /api/queue/{id}` (with reasoning),
`GET/POST /api/commitments[...]/done|/snooze`, `GET/POST /api/voice-profiles[/rebuild]`,
`GET /api/voice-profiles/{segment}/samples`, `POST /api/contacts/{email}/update`,
`GET /api/rules/proposed` + `POST /api/rules/{id}/confirm|reject`,
`GET /api/audit-log[/export]`, `POST /api/test-pipeline`,
`GET /api/metrics/daily|accuracy|costs|response-times`, `WS /ws/pipeline`.

Every write still routes through `assistant/web/service.py` → the same guarded seams.
Metrics reads prefer the nightly `metrics_cache` snapshot (`metrics.populate`, run 23:00
by the poller) and fall back to a live compute on a miss.

## Adding an endpoint + view
1. Add a read to `storage/read_queries.py` (or a guarded write to `web/service.py`).
2. Expose it in `web/api.py` (reads use `_cached(key, ttl, fn)`; writes call `_invalidate()`).
3. Add a method to `frontend/src/api.js`, render a component in `App.jsx`, add a `TABS` entry.

## Security
Both the API (`uvicorn ... host="127.0.0.1"`) and the WebSocket bind **localhost only** —
never `0.0.0.0`. There is no auth because the surface is not reachable off-machine; do not
expose port 8000/5173 publicly or via a tunnel. (The Gmail push receiver is the only
tunnel-facing port, and it only ever *wakes the poller* — it performs no DB or Gmail work.)
