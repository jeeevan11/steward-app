# PRODUCTION_READINESS.md

Production-readiness review for Steward, produced at the close of the Destruction →
Reconstruction program. Steward is a local-first AI chief-of-staff that triages Gmail +
WhatsApp and, **only on an explicit human approval tap**, sends replies.

**Readiness statement.** Every CRITICAL (2), HIGH (28), MEDIUM (44), and LOW (10) finding
in `DESTRUCTION_AUDIT.md` is **closed or explicitly accepted with rationale** (see
Accepted risks). The 14 machine-verifiable invariants in `PRODUCTION_INVARIANTS.md` all
hold (`scripts/verify_invariants.py`), and the full suite is green (**928 tests**). The
remaining gate before real users is **runtime validation under live load** (see Blind
spots) — this program was static + unit + failure-injection + an offline scale harness,
not a live soak test.

---

## 1. Trust guarantees (what a sophisticated user can rely on)

| Guarantee | Mechanism | Verified by |
|---|---|---|
| Nothing is sent without your tap | `begin_send` CAS; every send gated behind a `pending_actions` row | NO_AUTO_SEND, WEB_NO_AUTO_SEND |
| You can't approve A and send B | `approval_hash` re-checked before every send; a fold invalidates the approval | WYSIWYG_APPROVAL |
| A reply never duplicates | pre-send fail → `SEND_FAILED` (retryable); at/after-send → `SEND_AMBIGUOUS` (never auto-resent) | EXACTLY_ONCE_SEND |
| A reply never goes to the wrong thread/person | thread+recipient bound to the approved draft; Reply-To honored; Cc capped/sanitized | NO_WRONG_THREAD/RECIPIENT |
| No `[placeholder]` ever ships | send-path placeholder guard → `SEND_BLOCKED` | NO_PLACEHOLDER_SENT |
| No inbound mail silently vanishes | ledger state machine; history-gap resync widened + owner-notified | NO_SILENT_LOSS |
| Two people are never silently merged | exact phone/email/name+company only; everything else is a confirm-suggestion | IDENTITY_SAFETY |
| A stranger's claim isn't treated as truth | per-fact provenance (claimed/observed/inferred/verified); governance gates the read path | MEMORY_PROVENANCE |
| A hostile message can't steer the agent | untrusted content delimited as data; floors can't be lowered by spam verdicts | INJECTION_ISOLATION |
| "Off" means off | pause silences ingest, proactive, briefs, nudges | PAUSE_SILENCES_ALL |

## 2. Security posture

- **No-auto-send is structurally enforced**, not just by convention (CAS state machine).
- **Localhost-only binds.** The WhatsApp relay (`/send`, `/read`, `/send_media`,
  `/contacts`, `/resolve-lid`), the WhatsApp `/poll` receiver, and the Gmail Pub/Sub push
  receiver all require a shared secret (`INGEST_TOKEN` / `GMAIL_PUSH_TOKEN`); unauthenticated
  callers get 401. Web console mutations reject no-Origin/cross-origin POSTs (CSRF).
- **Least disclosure.** Server 500s return a generic body (details server-side only); the
  relay outbox is redacted (no cleartext message bodies) and size/age-capped; inline search
  answers only the configured owner.
- **Secrets** live in `.env` / `secrets/` (gitignored); `relay/outbox/`, `relay/outbox_out/`,
  contact caches, and runtime state are gitignored.
- **Fail-closed posture in live mode:** when `INGEST_TOKEN`/`GMAIL_PUSH_TOKEN` are unset
  the engine + relay log a loud, repeated warning (localhost-only deployments may still run
  open by choice).

## 3. Recovery guarantees (failure injection coverage)

| Failure | Behavior |
|---|---|
| Crash mid-send | row wedged in `SENDING` → `SEND_STUCK` reaper (keyed on a dedicated send-start clock); never resent |
| Lost/timed-out send ACK | `SEND_AMBIGUOUS`; owner told "could not confirm, did not resend"; explicit `force_resend` only |
| DB lock after delivery | `SEND_AMBIGUOUS` with the delivery id captured; never downgraded to retryable |
| Crash between dispatch and ledger.complete | side effects are idempotency-guarded (`process_side_effects` marker) — no duplicate episodes/LLM spend on replay |
| Gmail history expiry (>24h outage) | resync rescans `GMAIL_RESYNC_DAYS` (default 7) + records the gap + notifies the owner |
| Telegram/relay outage | undelivered cards re-delivered; relay health alerts; owner outbound buffered with durable retry |
| Card delivered-but-unpersisted | reconciliation log detects + heals without double-delivery |

## 4. Latency profile (from `scripts/profile_pipeline.py`, multi-year scale)

| Path | P50 | P99 |
|---|---|---|
| Live-queue render (`get_queue`, 10k rows) | **0.7 ms** | 1.1 ms |
| Settle planner (1k queued msgs) | 0.8 ms | 1.0 ms |
| Ledger dedup gate (`mark_seen`) | 0.02 ms | 0.05 ms |
| Dashboard `metrics_accuracy` (50k events) | 8.5 ms | 8.8 ms |
| Dashboard `learning_summary` (50k events) | **120 ms** | 165 ms |
| Retention prune (40k rows, one pass) | 27 ms | — |

The added indexes give a **~22× speedup** on the live-queue render at scale
(16.4 ms → 0.7 ms). LLM/network stages are network-bound (not benchmarkable offline) and
are bounded instead by `LLM_COST_BOUNDED` (daily cap + 429 breaker + media byte cap).

## 5. Scaling limits

- Validated to ~10k pending_actions and ~50k learning_events with sub-millisecond hot-path
  reads. The never-pruned tables (`pending_actions`, `learning_events`, `wa_messages`) now
  carry indexes; retention batches deletes and checkpoints the WAL so disk is reclaimed.
- **Known hotspot:** `learning_summary()` is ~120 ms at 50k events (a low-frequency dashboard
  read, not the hot path). Acceptable now; a candidate for pre-aggregation if the dashboard
  is polled aggressively. Tracked below.

## 6. Known limitations & accepted risks (explicit)

- **`ingest-whatsapp-6` (VIP burst coalescing) is opt-in, default off.** Coalescing a
  cross-poll VIP burst requires delaying the first message, which contradicts the instant-VIP
  guarantee. We chose latency over folding for VIPs; owners who prefer folding set
  `WHATSAPP_VIP_INSTANT_SETTLE_SECONDS>0`. **Rationale:** an investor/partner ping must arrive now.
- **launchd `KeepAlive`** is fixed in `com.cos.assistant.plist.template`; the same one-line
  fix should be applied to `com.cos.web.plist.template` / the cron plist before going live
  (noted by the audit, mechanical).
- **`learning_summary` latency** (see §5) — accepted as-is for now.
- The Mac app (SwiftUI) changes are parse-verified (`swiftc -parse`) but not exercised by an
  automated UI test; verify the menu-bar/console visually before shipping.

## 7. Blind spots (what was NOT done)

- **No live runtime / load / soak test** against real Gmail/WhatsApp/Telegram accounts.
- **No fuzzing** of the running process; no real LLM-output sampling against the prompts
  (injection isolation is enforced structurally + unit-tested, not red-teamed live).
- **No chaos testing** of the actual relay/engine processes (failure injection is at the
  function/state-machine level).
- Perf numbers are from an offline SQLite harness, not production hardware under concurrency.

## 8. Deployment checklist (before real users)

1. Set `INGEST_TOKEN` (same value in `.env` and the relay env) and `GMAIL_PUSH_TOKEN`.
2. Apply the `KeepAlive` plist fix to web/cron plists (§6).
3. Run `.venv/bin/python -m unittest discover -s tests` → expect OK (928).
4. Run `.venv/bin/python scripts/verify_invariants.py` → expect ALL HOLD (14).
5. Run `.venv/bin/python scripts/profile_pipeline.py` on target hardware; confirm hot-path P99 < a few ms.
6. Start in **dry-run** (`MODE=dry_run`); confirm classification + drafting, zero sends.
7. Soak in dry-run on the live account for a day; review the queue, briefs, and any
   `SEND_BLOCKED`/`SEND_AMBIGUOUS`/coverage-gap notifications.
8. Flip `MODE=live` only after the soak is clean. Send the first few approvals yourself and
   watch the thread/recipient on each.

## 9. Rollback checklist

- All reconstruction work is on the `reconstruction-hardening` branch; **master is
  untouched**. To roll back: `git checkout master` and restart the three processes.
- Schema changes are **additive only** (new columns/tables/indexes via idempotent migrations);
  the old code ignores them, so a rollback needs no down-migration.
- If a single subsystem misbehaves post-merge, its fixes are isolated in their own files
  (disjoint clusters) and individually revertable by file.
- Config is backward-compatible: every new knob has a safe default and an empty value
  reproduces legacy behavior.

---

*Generated at the close of the Reconstruction Program. Re-run §8.3–8.5 after any change to
keep this document honest.*
