# PRODUCTION_INVARIANTS.md

The machine-verifiable safety contract for Steward. Each invariant has a stable ID, a
precise statement, the code that enforces it, and the test(s) that verify it. An
invariant is only "held" when its verification row is green.

Run the whole contract (fast, deterministic):

```bash
.venv/bin/python scripts/verify_invariants.py     # the invariant gate (PASS/FAIL per invariant)
.venv/bin/python -m unittest discover -s tests    # the full suite (705 tests)
```

Status legend: **ENFORCED** (code + regression test in place) · **PARTIAL** (enforced
on the audited paths; a residual is tracked by a finding) · **OPEN** (not yet enforced).

| ID | Invariant | Verified by | Status |
|----|-----------|-------------|--------|
| NO_AUTO_SEND | A reply is transmitted only after an explicit human approval action. | `test_guardrails`, `test_web_csrf`, `test_personal_guardrail`, `test_send_safety` | ENFORCED |
| EXACTLY_ONCE_SEND | One approval never produces more than one delivery (retries, restarts, lost ACKs, DB locks). Pre-send failure → `SEND_FAILED` (retryable); at/after-send → `SEND_AMBIGUOUS` (never auto-resent); `SEND_STUCK` reaper. | `test_send_safety`, `test_reliability` | ENFORCED |
| WYSIWYG_APPROVAL | The sent draft hash-equals the approved draft; a fold mutating a draft under an approval is refused (`SEND_BLOCKED`) or re-rendered + re-approved. | `test_wysiwyg_approval`, `test_dispatch_fold_rerender` | ENFORCED |
| NO_WRONG_THREAD / NO_WRONG_RECIPIENT | A reply goes only to the approved thread + recipients; a fold that changes the target invalidates the approval; ambiguous compose recipients are not blasted. | `test_send_routing`, `test_compose_recipient_safety`, `test_memory_privacy_multirecipient` | ENFORCED |
| NO_PLACEHOLDER_SENT | A draft with an unresolved `[placeholder]`/`[your name]` sentinel can never be transmitted (guard at the send path). | `test_placeholder_guard` | ENFORCED |
| NO_SILENT_LOSS | Every inbound message ends surfaced / handled / archived / explicitly-failed-and-notified. Gmail history-gap resync widened + owner-notified. | `test_gmail_resync`, `test_reliability` | ENFORCED for outages ≤ `GMAIL_RESYNC_DAYS`; longer gaps surfaced |
| IDENTITY_SAFETY / NO_AUTO_MERGE | No two identifiers merge without an exact, unambiguous signal. Phone match is exact (no substring); name+domain alone is now a confirm-suggestion, not a silent merge. | `test_identity`, `test_identity_phone_safety`, `test_memory_integrity` | ENFORCED |
| MEMORY_PROVENANCE | Every durable fact carries source + source_type (claimed/observed/inferred/verified); a counterparty's claim is never rendered as verified truth; forged "you promised" commitments are not attributed to the owner. | `test_memory_integrity` | ENFORCED |
| INJECTION_ISOLATION | Untrusted message content is delimited as data and cannot issue instructions to the classifier/extractors or bypass guardrail floors; a confident-spam verdict can never lower a floor. | `test_classification_safety` | ENFORCED |
| RESPECT_DRY_RUN | In dry-run, nothing in Gmail/WhatsApp changes and nothing is sent. | `test_integration_dryrun` | ENFORCED |
| RELAY_AUTH / LOCALHOST_ONLY | All listeners bind `127.0.0.1`; relay HTTP (send + read paths) requires the shared `INGEST_TOKEN`. | `test_relay_auth`, `test_phone_contacts_relay_auth`, `test_miniapp_auth` | ENFORCED |
| WEB_NO_AUTO_SEND / CSRF | No-Origin / non-allowlisted-Origin POSTs to mutating endpoints are rejected; inline search answers only the configured owner. | `test_web_csrf`, `test_inline_search_auth` | ENFORCED |
| PAUSE_SILENCES_ALL / TRUTHFUL_STATE | A paused agent emits no proactive/brief/nudge output; state queries use real columns and rank by urgency, not age; reminder cards are truthful (no fake "send"). | `test_pause_suppression`, `test_state_engine_commitments`, `test_decision_ranking` | ENFORCED |
| NO_INTERNAL_LEAK / NO_SECRET_IN_LOGS | Server 500s return a generic body (details logged server-side only); the relay outbox is redacted (no cleartext bodies) and size/age-capped; blank-body sends are blocked. | `test_ux_web_display`, `test_relay_whatsapp_relay.mjs` | ENFORCED |
| INGEST_AUTH | The Gmail Pub/Sub push receiver and the WhatsApp `/poll` receiver reject unauthenticated callers. | `test_ingest_pipeline_hardening`, `test_whatsapp_llm_hardening` | ENFORCED |
| LLM_COST_BOUNDED | LLM calls honor a daily spend cap, a 429 circuit-breaker, and an input-media byte cap. | `test_llm_layer_hardening` | ENFORCED |

## The send state machine (after hardening)

```
PENDING ─approve→ APPROVED ─begin_send(CAS, stamps sending_started_at)→ SENDING
   │                                                          │
   │  (fold mutates draft → INVALIDATED approval)             ├─ integrity guard fails (WYSIWYG mismatch / placeholder) → SEND_BLOCKED  (re-Edit to recover; never plain re-Approve)
   │                                                          ├─ pre-send build error → SEND_FAILED      (provably not sent; retryable)
   │                                                          ├─ send raised / lost ACK → SEND_AMBIGUOUS (maybe delivered; NEVER auto-resent; force_resend_after_ambiguous only)
   │                                                          ├─ delivered, DB write fails → SEND_AMBIGUOUS (id captured)
   │                                                          ├─ wedged past cutoff → SEND_STUCK         (reaper; never resent)
   │                                                          └─ delivered + recorded → SENT
```
`SEND_BLOCKED`, `SEND_AMBIGUOUS`, `SEND_STUCK` are in **no** sendable set — `begin_send`
only promotes `APPROVED`/`EDITED`, so none of them can be auto-resent.

## Closed by the Reconstruction Program

**CRITICAL:** `ingest-email-1` (NO_SILENT_LOSS), `memory-identity-1` (IDENTITY_SAFETY).

**HIGH (28):** the EXACTLY_ONCE_SEND cluster (`autosend-invariant-1`, `storage-persistence-4/5`);
the APPROVAL-INTEGRITY cluster (`autosend-invariant-2`, `approval-telegram-1/2`,
`drafting-safety-1/2/3`, `failure-recovery-2`, `scaling-time-2`); the CLASSIFY cluster
(`classifier-brain-1/2/3`, `llm-layer-2`); the MEMORY cluster (`memory-knowledge-1/3/4`,
`memory-identity-2/4`); WEB (`web-security-1`, `approval-telegram-3`); RELAY
(`config-secrets-deploy-1`); CONTROL/UX (`control-state-presence-1/3`, `ux-trust-1/2`).

**MEDIUM/LOW (54):** all closed (53 fixed across 10 parallel clusters; `classifier-brain-5`
confirmed already-closed by `classifier-brain-1`). Covers ingest auth (Gmail push,
WhatsApp `/poll`), Reply-To/Cc routing, VIP burst handling, LLM cost/availability guards,
memory provenance read-side, identity LID resolution, learning decay/scoping, scaling
indexes + retention checkpointing, relay media typing + outbox redaction, pause/tz
correctness, and UX truthfulness (blank-send block, scoped clear-all with undo).

**Nothing left OPEN.** Every CRITICAL, HIGH, MEDIUM, and LOW finding in
`DESTRUCTION_AUDIT.md` is closed or explicitly accepted. Residual hardening that is
genuinely out of scope (e.g. applying the same launchd `KeepAlive` fix to the web/cron
plists) is noted in `PRODUCTION_READINESS.md`.
