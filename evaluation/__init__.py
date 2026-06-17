"""Permanent evaluation framework (Phase 4).

A labeled-benchmark harness that runs synthetic scenarios through the REAL brain
(classifier.classify_thread + tiers.decide) in dry-run on an in-memory SQLite DB,
compares the outcome to human labels, and reports accuracy / escalation / false
positive / false negative metrics. Mirrors test_flow.py's harness pattern so the
two stay consistent. Stdlib only; no network needed in --no-llm mode.
"""
