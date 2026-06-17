"""Evaluation runner: drive labeled scenarios through the REAL brain.

Mirrors test_flow.py's harness pattern (FakeLLM `--no-llm` mode, CallLog metrics
sink, an in-memory SQLite DB, the real classifier.classify_thread + tiers.decide),
but instead of pretty-printing one flow it loads JSONL benchmark files, compares
each outcome to its human label, and returns per-scenario records + aggregate
metrics (see metrics.compute_metrics).

Crucially the DB here is a FRESH in-memory DB seeded only from the scenario's own
``seed`` block. We never snapshot or touch the live DB, so results are deterministic
and the live inbox is never read or written.

Public API:
    load_dataset(path) -> list[dict]
    run_scenario(env, scenario) -> dict (a metrics-shaped per-scenario record)
    run_dataset(env, scenarios) -> list[dict]
    build_env(*, no_llm=True) -> dict   (settings + in-memory DB + llm + calllog)
    run_paths(paths, *, no_llm=True) -> dict   (the full result bundle)
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from typing import Any

from assistant.brain import classifier
from assistant.brain.tiers import TierConfig, decide as tiers_decide
from assistant.config import load_settings
from assistant.memory import contacts as memory_contacts
from assistant.memory import distill as distill_mod
from assistant.memory import identity, retrieval
from assistant.memory.distill import RelationshipMemory
from assistant.models import Channel, Message, Thread, Tier
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo

# Reuse the exact fake-LLM + call-capture primitives the observability harness uses,
# so the evaluation brain behaves identically to test_flow in --no-llm mode.
from evaluation.test_flow import CallLog, FakeLLM


# ─────────────────────────────────────────────────────────────────────────────
# Environment (in-memory DB, settings forced to dry-run, an LLM client)
# ─────────────────────────────────────────────────────────────────────────────
def build_env(*, no_llm: bool = True) -> dict[str, Any]:
    """A fresh in-memory DB + dry-run settings + the chosen LLM client.

    ``no_llm=True`` (the CI default) uses the deterministic FakeLLM. ``no_llm=False``
    constructs the real LLMClient (needs OPENROUTER_API_KEY) for an online run."""
    settings = dataclasses.replace(load_settings(), mode="dry_run")
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA busy_timeout=5000")
    db.init_db(mem)
    decision_log.ensure(mem)
    calllog = CallLog()
    if no_llm:
        llm = FakeLLM(calllog)
    else:  # pragma: no cover - exercised only in an online run
        from assistant.llm.client import LLMClient

        llm = LLMClient(settings, metrics_sink=calllog.sink)
    return {"mem": mem, "settings": settings, "llm": llm, "calllog": calllog, "no_llm": no_llm}


# ─────────────────────────────────────────────────────────────────────────────
# Dataset loading
# ─────────────────────────────────────────────────────────────────────────────
def load_dataset(path: str) -> list[dict[str, Any]]:
    """Read a JSONL benchmark file. Blank lines and lines starting with '#' are
    skipped so a dataset can carry comments. Malformed lines raise (a bad benchmark
    should fail loudly, not silently score nothing)."""
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{n}: invalid JSON ({exc})") from exc
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Builders (small, local — do not depend on test_flow's printing helpers)
# ─────────────────────────────────────────────────────────────────────────────
def _channel(name: str) -> str:
    return Channel.WHATSAPP if (name or "").lower() == "whatsapp" else Channel.GMAIL


def _build_thread(scenario: dict[str, Any]) -> Thread:
    ch = _channel(scenario.get("channel", "gmail"))
    sid = scenario["id"]
    m = Message(
        id=f"{sid}-1",
        thread_id=f"t-{sid}",
        channel=ch,
        sender_email=scenario.get("sender", ""),
        sender_name=scenario.get("sender_name", ""),
        recipients=["me@example.com"],
        subject=scenario.get("subject", ""),
        body_text=scenario.get("body", ""),
        snippet=(scenario.get("body", "") or "")[:160],
        timestamp=time.time(),
        from_me=False,
    )
    return Thread(id=f"t-{sid}", channel=ch, subject=scenario.get("subject", ""), messages=[m])


def _seed(env: dict[str, Any], scenario: dict[str, Any], thread: Thread, person_id: str) -> None:
    """Apply the scenario's ``seed`` + ``flags`` + ``importance`` to the in-memory DB.

    Mirrors the pre_seed/post_seed hooks in test_flow's scenarios:
      * flags        -> repo.add_contact_flag (investor/personal/mute/...)
      * importance   -> repo.bump_contact_importance (VIP floor)
      * seed.memory  -> a RelationshipMemory saved for the person
      * seed.episodes-> recorded agent episodes (surfaced/skipped) for suppression
    """
    mem = env["mem"]
    sender = scenario.get("sender", "")

    for flag in scenario.get("flags", []) or []:
        try:
            repo.add_contact_flag(mem, sender, flag)
        except Exception:  # noqa: BLE001 - seeding is best-effort
            pass

    seed = scenario.get("seed") or {}

    # importance may sit at the top level or inside ``seed`` (both accepted).
    importance = scenario.get("importance", seed.get("importance"))
    if importance:
        try:
            repo.bump_contact_importance(mem, sender, int(importance))
        except Exception:  # noqa: BLE001
            pass

    mem_seed = seed.get("memory")
    if mem_seed and person_id:
        try:
            rm = RelationshipMemory(person_id)
            rm.summary = dict(mem_seed.get("summary", {}))
            rm.decided = list(mem_seed.get("decided", []))
            os_seed = mem_seed.get("open_situation")
            if os_seed:
                rm.open_situations = [{
                    "key": os_seed.get("key", "s1"),
                    "situation": os_seed.get("situation", ""),
                    "awaiting": os_seed.get("awaiting", "nobody"),
                    "status": os_seed.get("status", "open"),
                    "thread_id": thread.id,
                    "last_activity_ts": int(time.time()),
                }]
            rm.last_distilled_at = int(time.time())
            distill_mod.save_memory(mem, rm)
        except Exception:  # noqa: BLE001
            pass

    for ep in seed.get("episodes", []) or []:
        if not person_id:
            break
        try:
            retrieval.record_episode(
                mem, person_id, action=ep.get("action", "surfaced"),
                tier=ep.get("tier"), thread_id=thread.id,
            )
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Run one scenario through the real pipeline
# ─────────────────────────────────────────────────────────────────────────────
def run_scenario(env: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """Run a single labeled scenario through identity -> memory -> classify -> decide
    and return a per-scenario record (the shape metrics.py expects).

    Suppression is detected exactly as test_flow does: compare the tier WITH memory
    signals to the tier WITHOUT them (or any ``memory:`` floor in applied_floors)."""
    mem, settings, llm = env["mem"], env["settings"], env["llm"]
    env["calllog"].reset()
    sid = scenario.get("id", "?")

    record: dict[str, Any] = {
        "id": sid,
        "expected_tier": int(scenario.get("expected_tier", 0)),
        "expected_category": scenario.get("expected_category", ""),
        "expected_suppressed": bool(scenario.get("expected_suppressed", False)),
        # An item is "consequential" (must reach the human) if its label says so or
        # its labeled tier is APPROVE+.
        "consequential": bool(scenario.get("consequential",
                                           int(scenario.get("expected_tier", 0)) >= int(Tier.APPROVE))),
        "actual_tier": None,
        "actual_category": "",
        "actual_suppressed": False,
        "applied_floors": [],
        "error": None,
    }

    try:
        thread = _build_thread(scenario)
        inbound = thread.latest_inbound or thread.latest

        # identity -> person (so memory + episodes can attach), best-effort.
        person_id = ""
        try:
            res = identity.resolve(mem, inbound)
            person_id = res.person_id or ""
        except Exception:  # noqa: BLE001
            person_id = ""

        _seed(env, scenario, thread, person_id)

        contact = memory_contacts.resolve_sender(mem, inbound)
        memory = distill_mod.load_memory(mem, person_id) if person_id else RelationshipMemory("")
        signals = (retrieval.memory_signals(mem, person_id, thread, contact, settings)
                   if person_id else retrieval.MemorySignals())
        block = retrieval.build_memory_block(memory)

        context = retrieval.get_context(mem, thread, contact)
        context.person_id = person_id
        context.memory_block = block

        # REAL classifier (three-step; canned outputs in --no-llm mode).
        decision = classifier.classify_thread(
            mem, llm, thread, context, prompts_dir=settings.prompts_dir)

        cfg = TierConfig.from_settings(settings)
        final = tiers_decide(thread, decision, contact, cfg, memory=signals)
        base = tiers_decide(thread, decision, contact, cfg, memory=None)

        suppressed = (
            any("memory:" in f for f in final.applied_floors)
            or int(final.final_tier) < int(base.final_tier)
        )

        record.update(
            actual_tier=int(final.final_tier),
            actual_category=str(decision.category),
            actual_suppressed=bool(suppressed),
            applied_floors=list(final.applied_floors),
            base_tier=int(base.final_tier),
        )
    except Exception as exc:  # noqa: BLE001 - never let one scenario crash the run
        record["error"] = f"{type(exc).__name__}: {exc}"

    return record


def run_dataset(env: dict[str, Any], scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run_scenario(env, s) for s in scenarios]


# ─────────────────────────────────────────────────────────────────────────────
# Top-level: run a set of dataset paths into a full result bundle
# ─────────────────────────────────────────────────────────────────────────────
def run_paths(paths: list[str], *, no_llm: bool = True) -> dict[str, Any]:
    """Run every dataset path and return one bundle:
        {
          "no_llm": bool,
          "datasets": { "<name>": {"records": [...], "metrics": {...}} },
          "metrics": {...},     # aggregate across all datasets
          "n_scenarios": int,
        }
    The aggregate metrics are computed over the pooled records so run_all.py can
    diff a single flat number set against the previous report.
    """
    from evaluation import metrics as metrics_mod

    env = build_env(no_llm=no_llm)
    bundle: dict[str, Any] = {"no_llm": no_llm, "datasets": {}, "n_scenarios": 0}
    all_records: list[dict[str, Any]] = []
    try:
        for path in paths:
            name = _dataset_name(path)
            scenarios = load_dataset(path)
            records = run_dataset(env, scenarios)
            all_records.extend(records)
            bundle["datasets"][name] = {
                "path": path,
                "records": records,
                "metrics": metrics_mod.compute_metrics(records, conn=env["mem"]),
            }
        bundle["n_scenarios"] = len(all_records)
        bundle["metrics"] = metrics_mod.compute_metrics(all_records, conn=env["mem"])
    finally:
        env["mem"].close()
    return bundle


def _dataset_name(path: str) -> str:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    return base[:-6] if base.endswith(".jsonl") else base
