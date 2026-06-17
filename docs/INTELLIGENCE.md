# INTELLIGENCE.md — how the brain works (P0–P5)

This is the map of the "smarter" layer added on top of the original triage engine.
Everything here is **additive** and **fail-safe**: any error or uncertainty surfaces
to you (Tier 3), nothing is ever silently auto-sent, and dry-run is respected.

---

## Model routing (TaskRouter — `llm/router.py`)

Every LLM call names a **task**, not a model. The router maps task → model + a
reasoning budget, so there is one place to tune the speed/quality/cost tradeoff:

| Task | Model | Thinking | Why |
|------|-------|----------|-----|
| `NOISE_FILTER` | flash | off | cheap, runs on every email |
| `THINK` | flash | 1024 | first-pass prep |
| `JUDGE` | flash | 2048 | the everyday decision |
| `JUDGE_CRITICAL` | **pro** | 8192 | investor/legal/money only |
| `SELF_CRITIQUE` | flash | off | safety re-check |
| `DRAFT` | draft model | off | reply prose |
| `COMMITMENT_EXTRACT` / `QUALITY_CHECK` | flash | off | utility passes |

Critical tasks (`JUDGE`, `JUDGE_CRITICAL`, `DRAFT_CRITICAL`) surface on fallback
instead of silently degrading. Per-call task/model/tokens/cost is logged to
`llm_calls` (feeds the Metrics view).

---

## Three-step reasoning (`brain/classifier.py`)

A message that isn't obvious noise goes through three steps:

1. **THINK** — a cheap first-pass reading that extracts entities, the real
   relationship, urgency signals, and ambiguities. *Never the decision.* If it
   fails or the model can't do it, we continue with no prep (never a crash).
2. **JUDGE** — the actual `Decision` (tier, category, stakes, reversibility,
   confidence…). Routed to **`JUDGE_CRITICAL`** (Gemini Pro + big reasoning budget)
   when `guardrails.is_critical(thread, contact)` is true — investor domains,
   investor terms, legal attachments, or an investor/legal contact. Invalid JSON →
   **fail-safe to Tier 3**.
3. **SELF_CRITIQUE** — a skeptic that can only **raise** the tier (0/+1/+2), never
   lower it. Skipped when the judge already failed safe. Invalid output → keep the
   judge's decision unchanged.

After all three, the deterministic **hard guardrails** in the tier engine still run
and are the last word. All three steps are stored in `decision_log`
(`think_output`, `judge_output`, `critique_output`, `critique_adjustment`,
`was_critical`) and shown in the dashboard's "How it decided" panel.

**Example.** A note from `partner@a venture firm.com` saying "let's discuss the term sheet":
THINK tags it `existing investor` + entity "term sheet"; `is_critical` is true so
JUDGE runs on Pro; the judge proposes Tier 2; SELF_CRITIQUE raises to Tier 3
("fundraising — confirm first"); guardrails independently floor it to Tier 3 too.
You get an "Ask" card with a pre-drafted suggested reply.

---

## Tuning the classifier for a new topic

Edit `prompts/classifier.md` — the standing-context block at the top is plain text
you can extend (who the principal is, what matters, what's noise). For hard rules
that must *never* be left to the model, add a **guardrail floor** instead (below) —
prompts guide, guardrails guarantee.

---

## The learning loop (`learning/recorder.py`, P5c)

Every human signal is captured immediately (fire-and-forget — never blocks an
action):

- **Approve (no edit)** → the draft is saved as a voice sample for that sender and
  their `importance` ticks up by 1.
- **Edit** → a `draft_edits` row stores the original, your final text, and a unified
  diff (segment-tagged) — this is how the system sees where your voice differs.
- **Skip** → a `skip_log` row; repeated skips for a sender/category make the updater
  **propose** a quieter rule.

Proposed rules land in `proposed_rules` (status `pending`) and the rules table
(`status='proposed'`). They are **never auto-applied** — you confirm/reject them in
Telegram or the dashboard's Rules view. Confirming a learned rule promotes it to an
active global rule.

---

## Adding a new guardrail floor (`brain/guardrails.py`)

Guardrails are pure functions that can only **raise** the tier. To add one, drop a
keyword set and a `bump()` in `evaluate()`:

```python
PRICING_TERMS = ("discount", "refund request", "chargeback")

# inside evaluate(), after the haystack is built:
if _contains_any(haystack, PRICING_TERMS):
    bump(Tier.APPROVE, "pricing/refund topic — review")
```

If the topic should also get the heavyweight model, add it to `is_critical()` too.
Add a test in `tests/test_guardrails.py` (assert it floors, and assert an ambiguous
word doesn't over-fire).

---

## Calendar + commitments (P4)

- **Calendar** (`memory/calendar_context.py`, opt-in via `CALENDAR_ENABLED`): a
  one-line free/busy summary is folded into the classifier prompt and the drafter
  gets real open slots to propose. Disabled or unreachable → empty, never blocks.
- **Commitments** (`memory/commitments.py`): after you send a reply, the model
  extracts explicit promises ("I'll send the deck by Friday"). The daily check
  (`COMMITMENT_CHECK_HOUR`) surfaces ones due soon or gone stale (VIP threshold 3
  days, others 5), plus threads that stalled after you replied. Buttons: Done /
  Snooze 2d / Draft follow-up.

---

## Segmented voice (P5a)

Replies are written in the voice for the recipient's **segment**
(`investor | customer | team | external`, via `contacts.detect_segment`). Profiles
are rebuilt weekly (Sunday 7pm) from your sent samples bucketed by segment; a
segment with fewer than 5 samples falls back to the global voice profile.

**Add a segment:** extend `SEGMENTS` and the rules in `detect_segment`
(`memory/contacts.py`), then it's automatically picked up by the rebuild and the
drafter.

---

## Draft quality gate (P5b — `action/quality_gate.py`)

Runs on every draft before it reaches you. **Never blocks.** Silently auto-fixes
em/en dashes and AI filler phrases; **flags** (but doesn't edit) possible fabricated
specifics (numbers/dates/amounts not grounded in the thread) and over-length drafts
per segment. Flags appear as a "⚠️ review" note on the card; the full result is
stored in `decision_log.quality_gate_result`.

---

## Speed (P0)

- **Push** (opt-in): set `GMAIL_PUBSUB_TOPIC`. Gmail `watch` publishes INBOX changes
  to Pub/Sub; a localhost receiver wakes the poller instantly (the history fetch +
  exactly-once ledger do the rest, so duplicate pushes are harmless). Setup needs a
  Cloud Pub/Sub topic + a push subscription pointed at a tunnel to
  `127.0.0.1:GMAIL_PUBSUB_PORT`. With no topic set, it polls — the always-on
  fallback. The watch auto-renews (~6-day cadence).
- **Pre-computation:** Tier 2 *and* Tier 3 cards carry a draft generated **before**
  the notification, persisted in `pending_actions` (survives restart). Approve is
  one tap, zero LLM calls on the critical path.
- **Optimistic UI:** a tap shows "Sending…" immediately; the send runs after; a
  failure offers Retry.
- **Latency is logged** (`response_times`): email→notification, tap→confirmation,
  draft generation — shown as p50/p95 in the Metrics view.
