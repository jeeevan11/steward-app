#!/usr/bin/env python3
"""Production-invariant verification gate.

Runs the regression tests that back each ENFORCED invariant in PRODUCTION_INVARIANTS.md
and prints a per-invariant PASS/FAIL. Exit code is non-zero if any enforced invariant
regresses, so this can be wired into CI / a pre-deploy check.

Usage:
    .venv/bin/python scripts/verify_invariants.py

This grows as the Reconstruction Program closes more findings: add the invariant id and
its test module(s) to INVARIANTS below as each becomes ENFORCED.
"""

from __future__ import annotations

import os
import sys
import unittest

# Allow running as `python scripts/verify_invariants.py` from anywhere: put the repo
# root (parent of scripts/) on sys.path so `assistant` and `tests` import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# invariant id -> (one-line statement, [test modules that verify it])
INVARIANTS: list[tuple[str, str, list[str]]] = [
    (
        "NO_SILENT_LOSS",
        "Gmail history-expiry resync recovers older mail + surfaces the gap (never silent).",
        ["tests.test_gmail_resync"],
    ),
    (
        "IDENTITY_SAFETY / NO_AUTO_MERGE_HIGH_CONFIDENCE",
        "Phone identity matching is exact (no substring), ambiguity yields no auto-merge.",
        ["tests.test_identity_phone_safety", "tests.test_identity"],
    ),
    (
        "EXACTLY_ONCE_SEND",
        "A maybe-delivered send is never auto-resent; only pre-send failures are retryable.",
        ["tests.test_send_safety", "tests.test_reliability"],
    ),
    (
        "WYSIWYG_APPROVAL",
        "The sent draft equals the approved draft; a fold under an approval is refused/re-rendered.",
        ["tests.test_wysiwyg_approval", "tests.test_dispatch_fold_rerender"],
    ),
    (
        "NO_WRONG_THREAD / NO_WRONG_RECIPIENT",
        "A reply goes only to the approved thread/recipients; ambiguous compose recipients are not blasted.",
        ["tests.test_send_routing", "tests.test_compose_recipient_safety", "tests.test_memory_privacy_multirecipient"],
    ),
    (
        "NO_PLACEHOLDER_SENT",
        "A draft with an unresolved placeholder sentinel can never be transmitted.",
        ["tests.test_placeholder_guard"],
    ),
    (
        "INJECTION_ISOLATION",
        "Untrusted message content cannot steer classification/extraction or bypass guardrail floors.",
        ["tests.test_classification_safety"],
    ),
    (
        "MEMORY_PROVENANCE",
        "Facts carry source/provenance; counterparty claims and forged commitments are not stored as truth.",
        ["tests.test_memory_integrity"],
    ),
    (
        "RELAY_AUTH / LOCALHOST_ONLY",
        "Relay HTTP calls require the shared INGEST_TOKEN on send and read paths.",
        ["tests.test_relay_auth", "tests.test_phone_contacts_relay_auth"],
    ),
    (
        "WEB_NO_AUTO_SEND / CSRF",
        "No-Origin POSTs to mutating endpoints are rejected; inline search answers only the owner.",
        ["tests.test_web_csrf", "tests.test_inline_search_auth"],
    ),
    (
        "PAUSE_SILENCES_ALL / TRUTHFUL_STATE",
        "A paused agent emits no proactive/brief output; state queries use real columns and rank by urgency.",
        ["tests.test_pause_suppression", "tests.test_state_engine_commitments", "tests.test_decision_ranking"],
    ),
    (
        "NO_INTERNAL_LEAK / NO_SECRET_IN_LOGS",
        "Server 500s return a generic body (details server-side only); the blank-body send is blocked.",
        ["tests.test_ux_web_display"],
    ),
    (
        "INGEST_AUTH",
        "The Gmail push receiver and the WhatsApp /poll receiver reject unauthenticated callers.",
        ["tests.test_ingest_pipeline_hardening", "tests.test_whatsapp_llm_hardening"],
    ),
    (
        "LLM_COST_BOUNDED",
        "LLM calls honor a daily spend cap, a 429 circuit-breaker, and an input-media byte cap.",
        ["tests.test_llm_layer_hardening"],
    ),
]


def _run(modules: list[str]) -> tuple[bool, int, int]:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for m in modules:
        suite.addTests(loader.loadTestsFromName(m))
    result = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w")).run(suite)
    ran = result.testsRun
    failed = len(result.failures) + len(result.errors)
    return failed == 0, ran, failed


def main() -> int:
    print("=" * 72)
    print("STEWARD PRODUCTION-INVARIANT GATE")
    print("=" * 72)
    all_ok = True
    for inv_id, statement, modules in INVARIANTS:
        ok, ran, failed = _run(modules)
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_ok = False
        print(f"[{status}] {inv_id}")
        print(f"        {statement}")
        print(f"        ({ran} checks, {failed} failed; {', '.join(modules)})")
    print("-" * 72)
    print("RESULT:", "ALL ENFORCED INVARIANTS HOLD ✅" if all_ok else "INVARIANT REGRESSION ❌")
    print("=" * 72)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
