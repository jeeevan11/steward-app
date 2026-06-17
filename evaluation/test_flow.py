#!/usr/bin/env python3
"""test_flow.py — pipeline observability harness.

Runs ten synthetic scenarios through the REAL decision-path functions in dry-run mode
and prints the whole flow in plain English, so you can watch the system think.

This is NOT the unit-test suite (that's `tests/`). It is an end-to-end *observability*
tool. It composes the real pipeline functions in the real order:

    identity.resolve → distill.load_memory → retrieval.build_memory_block / memory_signals
    → classifier.classify_thread → guardrails.evaluate → tiers.decide
    → drafting.draft_reply → quality_gate.check_and_fix → distill.distill

It deliberately does NOT call main.process_one / dispatcher.dispatch / ledger.* / any
notifier — those are the side-effecting, Telegram-notifying parts. It is dry-run and
silent by construction, and it never writes the live database (it works on an
in-memory snapshot).

Usage:
    python test_flow.py                 # all ten, real LLM
    python test_flow.py --scenario 3
    python test_flow.py --scenario 5,6,7
    python test_flow.py --no-llm        # structure only, no tokens / no network
    python test_flow.py --verbose       # also print the memory block + assembled context
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import time
import traceback

from assistant.brain import classifier, guardrails
from assistant.brain.tiers import TierConfig, decide as tiers_decide
from assistant.config import load_settings
from assistant.memory import commitments as commitments_mod
from assistant.memory import contacts as memory_contacts
from assistant.memory import distill as distill_mod
from assistant.memory import identity, retrieval
from assistant.memory.distill import RelationshipMemory
from assistant.models import Channel, Message, Thread, Tier
from assistant.storage import db, decision_log
from assistant.storage import repositories as repo

HR = "═" * 60
SUB = "─" * 60
TIER_LABEL = {0: "SILENT", 1: "FYI", 2: "APPROVE", 3: "ASK"}


# ─────────────────────────────────────────────────────────────────────────────
# LLM call capture (no DB writes — never use metrics.make_sink here)
# ─────────────────────────────────────────────────────────────────────────────
class CallLog:
    def __init__(self):
        self.calls: list[dict] = []
        self.t0 = time.monotonic()

    def sink(self, rec: dict) -> None:          # passed to the real LLMClient
        r = dict(rec)
        r["ts"] = time.monotonic()
        self.calls.append(r)

    def reset(self) -> None:
        self.calls = []
        self.t0 = time.monotonic()

    def find(self, *tasks: str):
        for c in self.calls:
            if c.get("task") in tasks:
                return c
        return None

    def dur(self, call: dict):
        if call is None:
            return None
        i = self.calls.index(call)
        prev = self.calls[i - 1]["ts"] if i > 0 else self.t0
        return max(0.0, call["ts"] - prev)

    def cost(self) -> float:
        return sum(float(c.get("cost") or 0.0) for c in self.calls)


class FakeLLM:
    """--no-llm stand-in. Returns canned, valid JSON for each step and records a call
    so the per-step display is uniform. The DETERMINISTIC parts of the pipeline
    (guardrails, suppression, identity, memory signals) still run for real."""

    def __init__(self, calllog: CallLog):
        self.cl = calllog

    def _emit(self, task: str) -> None:
        self.cl.calls.append({"task": task, "model": "(no-llm)", "prompt_tokens": 0,
                              "completion_tokens": 0, "cost": 0.0, "ts": time.monotonic()})

    def noise_pass(self, *, system_prefix, thread_text, schema, message_id=""):
        self._emit("NOISE_FILTER")
        markers = ("unsubscribe", "techcrunch", "newsletter", "digest", "view in browser")
        is_noise = any(m in thread_text.lower() for m in markers)
        return json.dumps({"is_noise": is_noise, "confidence": 0.95 if is_noise else 0.2,
                           "label": "Newsletters" if is_noise else "", "reason": "(no-llm heuristic)"})

    def think(self, *, system_prefix, thread_text, schema, message_id=""):
        self._emit("THINK")
        return json.dumps({"key_entities": [], "relationship_context": "(no-llm)",
                           "urgency_signals": [], "ambiguities": [], "preliminary_category": "other"})

    def classify(self, *, system_prefix, thread_text, schema, effort="high", task="JUDGE", message_id=""):
        self._emit(task)
        return json.dumps({
            "category": "work_request", "intent": "(no-llm) requests action",
            "sender_importance": 40, "stakes": "medium", "reversibility": "reversible",
            "proposed_tier": 2, "confidence": 0.7, "needs_reply": True,
            "reasoning": "(no-llm canned decision; real run produces the real judgment)",
            "suggested_action": "reply", "one_line_summary": "(no-llm) summary",
            "memory_conflict": False,
        })

    def self_critique(self, *, system_prefix, user_text, schema, message_id=""):
        self._emit("SELF_CRITIQUE")
        return json.dumps({"tier_adjustment": 0, "reason": "(no-llm)"})

    def draft(self, *, system_prefix, user_prompt, max_tokens=1200, effort="high", task="DRAFT", message_id=""):
        self._emit(task)
        return "[no-llm draft placeholder. The real run writes the actual reply in Jatin's voice.]"

    def complete_json(self, *, task, system_prefix, user_text, schema, max_tokens=700, message_id=""):
        self._emit(task)
        if task == "COMMITMENT_EXTRACT":
            return json.dumps({"commitments": [
                {"commitment_text": "send the updated deck and financials", "due_date_hint": None, "contact_email": None},
                {"commitment_text": "connect them with our CTO", "due_date_hint": None, "contact_email": None},
            ]})
        return json.dumps({"facts": [], "open_situations": [], "decided": []})

    def transcribe(self, **kw):  # not exercised by these scenarios
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# DB snapshot (read-only source → in-memory; the live DB is NEVER written)
# ─────────────────────────────────────────────────────────────────────────────
def snapshot_db(db_path: str) -> sqlite3.Connection:
    mem = sqlite3.connect(":memory:")
    try:
        ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ro.backup(mem)
        ro.close()
        print(f"   snapshot: copied live DB ({db_path}) read-only into memory")
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️  could not snapshot live DB ({exc}); running on an empty in-memory DB")
    mem.row_factory = sqlite3.Row
    mem.execute("PRAGMA busy_timeout=5000")
    db.init_db(mem)            # ensure every table exists (idempotent)
    decision_log.ensure(mem)   # ensure the reasoning columns exist
    return mem


# ─────────────────────────────────────────────────────────────────────────────
# Builders + small helpers
# ─────────────────────────────────────────────────────────────────────────────
def msg(mid, *, sender, name="", body="", subject="", channel=Channel.GMAIL,
        from_me=False, ts=None, recipients=None) -> Message:
    return Message(
        id=mid, thread_id=f"t-{mid}", channel=channel,
        sender_email=("" if from_me else sender), sender_name=name,
        recipients=recipients or ["me@example.com"], subject=subject, body_text=body,
        snippet=body[:160], timestamp=ts if ts is not None else time.time(), from_me=from_me,
    )


def thread_of(tid, messages, subject="", channel=Channel.GMAIL) -> Thread:
    for m in messages:
        m.thread_id = tid
    return Thread(id=tid, channel=channel, subject=subject, messages=messages)


def cost_s(c: float) -> str:
    return f"${c:.4f}"


def dur_s(d) -> str:
    return f"~{d:.1f}s" if d is not None else "~?"


def model_of(call) -> str:
    return call.get("model", "?") if call else "(not called)"


def _short(text, n=240):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n].rstrip() + " …"


def _pretty(raw):
    try:
        return json.dumps(json.loads(raw), indent=2)[:600] if raw else "(none)"
    except (ValueError, TypeError):
        return _short(str(raw), 600)


def tier_str(t) -> str:
    return f"{int(t)} ({TIER_LABEL.get(int(t), '?')})"


# ─────────────────────────────────────────────────────────────────────────────
# Printing the flow for one inbound scenario
# ─────────────────────────────────────────────────────────────────────────────
def run_inbound(env, num, title, thread, *, pre_seed=None, post_seed=None,
                note_after_suppress=None, note_after_draft=None):
    """Run the standard inbound decision flow and print every step. Returns a summary row."""
    mem, settings, llm, cl = env["mem"], env["settings"], env["llm"], env["calllog"]
    cl.reset()
    t0 = time.monotonic()
    inbound = thread.latest_inbound or thread.latest

    print(f"\n{HR}\nSCENARIO {num} — {title}\n{HR}")
    print("\n📬 WHAT ARRIVED")
    ch = "WhatsApp" if thread.channel == Channel.WHATSAPP else "Email"
    print(f"   From: {inbound.sender_name or inbound.sender_email} <{inbound.sender_email}>  [{ch}]")
    if inbound.subject:
        print(f"   Subject: {inbound.subject}")
    print(f"   Body: \"{_short(inbound.body_text, 220)}\"")
    if len(thread.messages) > 1:
        print(f"   (thread of {len(thread.messages)} messages, oldest → newest)")

    if pre_seed:
        pre_seed(env, inbound, thread)

    # ── identity ──
    person_id = ""
    link_note = None
    try:
        res = identity.resolve(mem, inbound)
        person_id = res.person_id or ""
        if res.suggestion:
            link_note = f"would ask once: same person as {res.suggestion.get('candidate_name')}?"
    except Exception as exc:  # noqa: BLE001
        print(f"   ⚠️  identity.resolve failed ({exc}); continuing on the thread alone")

    if post_seed and person_id:
        post_seed(env, inbound, thread, person_id)

    contact = memory_contacts.resolve_sender(mem, inbound)
    memory = distill_mod.load_memory(mem, person_id) if person_id else RelationshipMemory("")
    signals = (retrieval.memory_signals(mem, person_id, thread, contact, settings)
               if person_id else retrieval.MemorySignals())
    block = retrieval.build_memory_block(memory)

    print("\n🧠 MEMORY LOADED")
    print(f"   Person: {memory.person_id or '(new / unresolved)'}"
          + (f"  ·  {link_note}" if link_note else ""))
    print(f"   Known facts: {memory.summary or '{}'}")
    open_now = [s.get('situation') for s in memory.open_situations if s.get('status') != 'resolved']
    print(f"   Open situations: {open_now or '[]'}")
    print(f"   Recently skipped: {'YES' if signals.recently_skipped else 'no'}"
          f" | Resolved: {'YES' if signals.situation_resolved else 'no'}"
          f" | Personal: {'YES' if signals.is_personal else 'no'}"
          f" | Stale: {'YES' if retrieval._is_stale(memory, time.time()) else 'no'}")

    context = retrieval.get_context(mem, thread, contact)
    context.person_id = person_id
    context.memory_block = block
    if env["verbose"]:
        print("\n   ── VERBOSE: memory block fed to the brain ──")
        print("   " + (block or "(empty)").replace("\n", "\n   "))
        print("   ── VERBOSE: assembled context ──")
        print("   " + context.render_for_prompt().replace("\n", "\n   ")[:1200])

    # ── classify (real three-step; reasoning persisted to decision_log) ──
    decision = classifier.classify_thread(mem, llm, thread, context, prompts_dir=settings.prompts_dir)
    dl = decision_log.get(mem, inbound.id)
    short_circuit = cl.find("THINK") is None   # noise pass short-circuited before THINK

    noise_call = cl.find("NOISE_FILTER")
    print(f"\n🔍 STEP 1 — NOISE CHECK  ({model_of(noise_call)} | {dur_s(cl.dur(noise_call))} | {cost_s(noise_call['cost'] if noise_call else 0)})")
    if short_circuit:
        print(f"   Result: NOISE → filed as {decision.category}. Pipeline short-circuits here.")
    else:
        print("   Result: NOT NOISE → proceeds to full classification.")

    if not short_circuit:
        think_call = cl.find("THINK")
        print(f"\n💭 STEP 2 — THINK  ({model_of(think_call)} | {dur_s(cl.dur(think_call))})")
        think_out = dl["think_output"] if dl and "think_output" in dl.keys() else ""
        print("   " + _pretty(think_out).replace("\n", "\n   "))

        judge_call = cl.find("JUDGE_CRITICAL", "JUDGE")
        crit = " — JUDGE_CRITICAL" if (judge_call and judge_call["task"] == "JUDGE_CRITICAL") else ""
        print(f"\n⚖️  STEP 3 — JUDGE  ({model_of(judge_call)}{crit} | {dur_s(cl.dur(judge_call))} | {cost_s(judge_call['cost'] if judge_call else 0)})")
        print(f"   Category: {decision.category} | Stakes: {decision.stakes} | "
              f"Reversible: {decision.reversibility}")
        print(f"   Proposed tier: {decision.proposed_tier} | Confidence: {decision.confidence:.2f} "
              f"| memory_conflict: {str(decision.memory_conflict).lower()}")
        print(f"   Reasoning: {_short(decision.reasoning, 200)}")

        critique_call = cl.find("SELF_CRITIQUE")
        adj = dl["critique_adjustment"] if dl and "critique_adjustment" in dl.keys() else 0
        print(f"\n🤔 STEP 4 — SELF-CRITIQUE  ({model_of(critique_call)} | {dur_s(cl.dur(critique_call))})")
        print(f"   Tier adjustment: +{adj} ({'raised' if adj else 'confirmed, no change'})")

    # ── guardrails ──
    g = guardrails.evaluate(thread, decision, contact, memory=signals)
    print("\n🛡️  STEP 5 — GUARDRAILS")
    if g.reasons:
        for r in g.reasons:
            print(f"   • {r}")
    else:
        print("   • no floors fired")
    print(f"   memory_conflict: {str(decision.memory_conflict).lower()} | "
          f"is_personal: {str(signals.is_personal).lower()}")
    print(f"   Final floor: {tier_str(g.floor)}")

    # ── decide (with + without memory, to show suppression) ──
    cfg = TierConfig.from_settings(settings)
    final = tiers_decide(thread, decision, contact, cfg, memory=signals)
    base = tiers_decide(thread, decision, contact, cfg, memory=None)
    suppressed = any("memory:" in f for f in final.applied_floors) or int(final.final_tier) < int(base.final_tier)
    print("\n🔇 STEP 6 — NUDGE SUPPRESSION")
    blockers = []
    if decision.memory_conflict:
        blockers.append("memory_conflict")
    if signals.is_personal:
        blockers.append("personal")
    if decision.stakes == "high":
        blockers.append("high stakes")
    if decision.reversibility != "reversible":
        blockers.append("irreversible")
    if suppressed:
        print(f"   Suppressed: YES → would-be {tier_str(base.final_tier)} lowered to {tier_str(final.final_tier)}")
    elif (signals.recently_skipped or signals.situation_resolved):
        why = f" (blocked by: {', '.join(blockers)})" if blockers else " (nothing above the floor to lower, or held by floor)"
        print(f"   Suppressed: NO{why} — stays {tier_str(final.final_tier)}")
    else:
        print(f"   Suppressed: no (no known-handled signals) — stays {tier_str(final.final_tier)}")
    if note_after_suppress:
        print(f"   {note_after_suppress}")

    print("\n✅  STEP 7 — FINAL DECISION")
    surfaced = "would surface to Jatin" if int(final.final_tier) >= 2 else "handled quietly"
    print(f"   Tier: {tier_str(final.final_tier)}  →  {surfaced}")
    if final.surfaced_reason:
        print(f"   Why surfaced: {final.surfaced_reason}")

    # ── draft + quality gate (tier >= APPROVE only) ──
    if int(final.final_tier) >= 2 and not short_circuit:
        draft = drafting_draft(mem, llm, settings, thread, contact, final)
        draft_call = cl.find("DRAFT_CRITICAL", "DRAFT")
        print(f"\n✍️  STEP 8 — DRAFT  ({model_of(draft_call)} | {dur_s(cl.dur(draft_call))} | {cost_s(draft_call['cost'] if draft_call else 0)})")
        print("   " + _short(draft, 400).replace("\n", "\n   "))

        seg = memory_contacts.detect_segment(mem, inbound.sender_email)
        from assistant.action import quality_gate
        qr = quality_gate.check_and_fix(draft, seg, thread.render_for_prompt())
        print("\n🔎 STEP 9 — QUALITY GATE")
        print(f"   Segment: {seg} | Auto-fixed: {qr.auto_fixed or 'none'}")
        print(f"   Flags: {qr.flags or 'none'} | Needs review: {str(qr.needs_review).lower()}")
        print(f"   Result: {'NEEDS REVIEW' if qr.needs_review else 'PASS'}")
        if note_after_draft:
            print(f"\n   {note_after_draft}")

    # ── memory update (distill; skip pure noise, like production) ──
    print("\n💾 STEP 10 — MEMORY UPDATE")
    if person_id and decision.category not in distill_mod.NOISE_CATEGORIES:
        before_v = memory.version
        before_decided = list(memory.decided)
        distill_mod.distill(mem, llm, settings, person_id, thread)
        after = distill_mod.load_memory(mem, person_id)
        new_facts = {k: v for k, v in after.summary.items() if memory.summary.get(k) != v}
        new_decided = [d.get("decision") for d in after.decided if d not in before_decided]
        print(f"   Distilled. Version: {before_v} → {after.version}")
        if new_facts:
            print(f"   Facts changed: {new_facts}")
        if new_decided:
            print(f"   New decided: {new_decided}")
        if not new_facts and not new_decided:
            print("   (no material change recorded)")
    else:
        print(f"   Skipped (category '{decision.category}' is noise, or no person resolved).")

    total = time.monotonic() - t0
    print(f"\n⏱️  Total: {dur_s(total)} | {cost_s(cl.cost())}")

    # summary row
    g_tag = "no"
    if int(g.floor) >= 2:
        joined = " ".join(g.reasons).lower()
        for key, label in (("investor", "inv"), ("personal", "pers"), ("memory conflict", "conflict"),
                           ("money", "money"), ("legal", "legal"), ("hardware", "hw"), ("acme", "acme")):
            if key in joined:
                g_tag = f"YES({label})"
                break
        else:
            g_tag = "YES"
    if suppressed:
        supp = "YES"
    elif blockers and (signals.recently_skipped or signals.situation_resolved):
        supp = "NO*"
    else:
        supp = "no"
    memory_influenced = bool(person_id) and (
        not memory.is_empty() or signals.is_personal
        or signals.recently_skipped or signals.situation_resolved
    )
    memtag = "yes" if memory_influenced else "none"
    return {"num": num, "title": title, "tier": int(final.final_tier), "suppressed": supp,
            "guardrail": g_tag, "memory": memtag, "cost": cl.cost(),
            "calls": len(cl.calls), "final": final, "ok": True}


def drafting_draft(mem, llm, settings, thread, contact, final):
    from assistant.action import drafting
    return drafting.draft_reply(mem, llm, settings, thread, contact, final)


# ─────────────────────────────────────────────────────────────────────────────
# The ten scenarios
# ─────────────────────────────────────────────────────────────────────────────
def scenario_1(env):
    body = ("The Latest from TechCrunch\n\nTop stories today: a new AI startup raised "
            "a Series B, Apple announced earnings, and the week in venture. Read more on "
            "our site. You are receiving this because you subscribed. Unsubscribe | "
            "View in browser.")
    th = thread_of("flow-s1", [msg("flow-s1-1", sender="updates@techcrunch.com",
                                    name="TechCrunch", subject="Your TechCrunch Daily", body=body)])
    return run_inbound(env, 1, "Newsletter / noise", th)


def scenario_2(env):
    body = ("Hey, our payment integration just broke. We have 200 merchants waiting and "
            "transactions are failing. This is urgent, we need a fix ASAP. Can you take a look?")
    th = thread_of("flow-s2", [msg("flow-s2-1", sender="rohan@customerpay.in",
                                    name="Rohan", subject="Payment integration down", body=body)])
    return run_inbound(env, 2, "Customer complaint (cold start)", th)


def scenario_3(env):
    body = ("Hey Jatin, quick check on how you're thinking about runway heading into Q3? "
            "Happy to jump on a call if that's easier.")
    th = thread_of("flow-s3", [msg("flow-s3-1", sender="priya@peakvc.com",
                                    name="Priya Nair", subject="Quick check-in on runway", body=body)])

    def pre(env, inbound, thread):
        repo.add_contact_flag(env["mem"], "priya@peakvc.com", "investor")

    return run_inbound(env, 3, "Investor asking about runway", th, pre_seed=pre)


def scenario_4(env):
    body = "Any update on the payment issue? It's been 2 hours and merchants are still blocked."
    th = thread_of("flow-s4", [msg("flow-s4-1", sender="rohan@customerpay.in",
                                    name="Rohan", subject="Re: Payment integration down", body=body)])

    def post(env, inbound, thread, person_id):
        m = RelationshipMemory(person_id)
        m.summary = {"company": "CustomerPay", "relationship": "customer"}
        m.open_situations = [{"key": "pay-int", "situation": "payment integration broken",
                              "awaiting": "owner", "status": "open", "thread_id": thread.id,
                              "last_activity_ts": int(time.time())}]
        m.last_distilled_at = int(time.time())
        distill_mod.save_memory(env["mem"], m)

    note = ("✅ The decision AND the draft now use memory: the open situation with Rohan is in "
            "the drafting prompt, so the reply picks up the known thread instead of starting "
            "cold. (This was the S4 finding; memory-aware drafting has since shipped.)")
    return run_inbound(env, 4, "Customer follow-up (memory populated)", th,
                       post_seed=post, note_after_draft=note)


def scenario_5(env):
    body = ("Just following up on my previous message. Wanted to check if you had a chance "
            "to review when you get a moment, no rush.")
    th = thread_of("flow-s5", [msg("flow-s5-1", sender="vendor@genericfollow.com",
                                    name="Sam", subject="Following up", body=body)])

    def post(env, inbound, thread, person_id):
        # this thread was surfaced and Jatin skipped it ~6h ago (within cooldown)
        retrieval.record_episode(env["mem"], person_id, action="surfaced", tier=2, thread_id=thread.id)
        retrieval.record_episode(env["mem"], person_id, action="skipped", tier=2, thread_id=thread.id)

    note = ("ℹ️ If this were a hardware/investor contact, the guardrail floor would block "
            "suppression — correctly. Suppression only fires when nothing else raises the floor.")
    return run_inbound(env, 5, "Recently skipped follow-up (suppression)", th,
                       post_seed=post, note_after_suppress=note)


def scenario_6(env):
    body = ("Great speaking yesterday. Just confirming we're moving forward with the order as "
            "discussed, will send the invoice shortly.")
    th = thread_of("flow-s6", [msg("flow-s6-1", sender="vendor@genericfollow.com",
                                    name="Sam", subject="Confirming the order", body=body)])

    def post(env, inbound, thread, person_id):
        m = RelationshipMemory(person_id)
        m.summary = {"relationship": "vendor"}
        m.decided = [{"decision": "declined this vendor, told them no on 1 Feb 2026",
                      "ts": int(time.time()) - 90 * 86400, "source_message_id": ""}]
        m.open_situations = [{"key": "order", "situation": "vendor order", "awaiting": "nobody",
                              "status": "resolved", "thread_id": thread.id,
                              "last_activity_ts": int(time.time())}]
        m.last_distilled_at = int(time.time())
        distill_mod.save_memory(env["mem"], m)

    note = ("(memory_conflict is the model's call — shown as-is. Even if the model misses it, "
            "the 'invoice' money keyword still surfaces this. Watch which floor fires.)")
    return run_inbound(env, 6, "Memory conflict (message contradicts a stored decision)", th,
                       post_seed=post, note_after_suppress=note)


def scenario_7(env):
    body = "Jatin bhai when are you coming home this weekend?"
    th = thread_of("flow-s7", [msg("flow-s7-1", sender="919999988888@s.whatsapp.net",
                                    name="Family", body=body, channel=Channel.WHATSAPP)],
                   channel=Channel.WHATSAPP)

    def pre(env, inbound, thread):
        repo.add_contact_flag(env["mem"], "919999988888@s.whatsapp.net", "personal")

    return run_inbound(env, 7, "Personal contact (hard floor)", th, pre_seed=pre)


def scenario_8(env):
    """Cross-channel identity: a WhatsApp message whose body contains an email that
    already belongs to a known person → strong auto-link."""
    mem = env["mem"]
    print(f"\n{HR}\nSCENARIO 8 — Cross-channel identity link\n{HR}")
    # seed the existing email-based person
    repo.person_add(mem, person_id="flow-amit", display_name="Amit",
                    emails=["amit@mfgsupply.com"], phone_jids=[], company="mfgsupply.com")
    repo.person_link_set(mem, "amit@mfgsupply.com", "flow-amit", source="observed")

    body = ("Hi, this is Amit from MFG Supplies. You can also reach me at amit@mfgsupply.com "
            "if that's easier.")
    inbound = msg("flow-s8-1", sender="918888877777@s.whatsapp.net", name="Amit",
                  body=body, channel=Channel.WHATSAPP)
    th = thread_of("flow-s8", [inbound], channel=Channel.WHATSAPP)

    print("\n📬 WHAT ARRIVED")
    print(f"   From: {inbound.sender_email}  [WhatsApp]")
    print(f"   Body: \"{_short(body, 200)}\"")
    print("\n🔗 IDENTITY RESOLUTION")
    res = identity.resolve(mem, inbound)
    linked = (res.person_id == "flow-amit" and not res.created)
    if linked:
        print("   🔗 LINKED: WhatsApp JID → person 'flow-amit' (amit@mfgsupply.com) via "
              "email-in-body (strong signal).")
    elif res.suggestion:
        print(f"   Suggested (weak): would ask once before linking → {res.suggestion}")
    else:
        print(f"   Created a NEW person {res.person_id} (no strong signal matched).")
    print(f"\n⏱️  Total: ~0.0s | {cost_s(0)}")
    return {"num": 8, "title": "Cross-channel link", "tier": "—",
            "suppressed": "—", "guardrail": "—", "memory": "LINKED" if linked else "new",
            "cost": 0.0, "calls": 0, "final": None, "ok": True}


def scenario_9(env):
    base = int(time.time()) - 6 * 3600
    msgs = [
        msg("flow-s9-1", sender="deal@partnerco.com", name="Partner", subject="Partnership",
            body="Would love to partner on this, big fan of what you're building.", ts=base + 0),
        msg("flow-s9-2", sender="", from_me=True, body="Thanks, that means a lot. Let's explore it.", ts=base + 60),
        msg("flow-s9-3", sender="deal@partnerco.com", name="Partner",
            body="Agreed on the terms you proposed. Let's plan to sign next week.", ts=base + 120),
        msg("flow-s9-4", sender="", from_me=True, body="Great, sending the draft contract over.", ts=base + 180),
        msg("flow-s9-5", sender="deal@partnerco.com", name="Partner",
            body="Draft looks good, we're excited to move forward.", ts=base + 240),
        msg("flow-s9-6", sender="deal@partnerco.com", name="Partner",
            body="Actually, we've decided to go with another vendor. Sorry for the confusion, "
                 "best of luck.", ts=base + 300),
    ]
    th = thread_of("flow-s9", msgs, subject="Partnership")
    return run_inbound(env, 9, "Long thread, last message reverses everything", th)


def scenario_10(env):
    """Commitment extraction from a SENT reply (a different sub-pipeline)."""
    mem, settings, llm, cl = env["mem"], env["settings"], env["llm"], env["calllog"]
    cl.reset()
    t0 = time.monotonic()
    print(f"\n{HR}\nSCENARIO 10 — Commitment extracted from a sent reply\n{HR}")
    sent = ("Thanks for the context. I'll send the updated deck and financials by Thursday EOD. "
            "Also connecting you with our CTO by next week.")
    contact_email = "advisor@fund.com"
    print("\n📤 SENT BY JATIN")
    print(f"   To: {contact_email}")
    print(f"   Body: \"{_short(sent, 220)}\"")

    found = commitments_mod.extract_commitments(llm, settings, sent, contact_email)
    extract_call = cl.find("COMMITMENT_EXTRACT")
    print(f"\n🧩 STEP — EXTRACT COMMITMENTS  ({model_of(extract_call)} | {dur_s(cl.dur(extract_call))} | {cost_s(extract_call['cost'] if extract_call else 0)})")
    if not found:
        print("   (none found)")
    for c in found:
        due = f" (due {c['due_date']})" if c.get("due_date") else ""
        print(f"   • {c['commitment_text']}{due}")
        commitments_mod.add_commitment(mem, message_id="flow-s10", contact_email=c["contact_email"] or contact_email,
                                       commitment_text=c["commitment_text"], due_date=c.get("due_date", ""))

    stored = commitments_mod.open_commitments(mem)
    print(f"\n💾 STORED — open commitments now in the (snapshot) DB: {len(stored)}")
    for r in stored[-len(found):] if found else []:
        print(f"   • {r['commitment_text']}  → {r['contact_email']}")
    print(f"\n⏱️  Total: {dur_s(time.monotonic() - t0)} | {cost_s(cl.cost())}")
    return {"num": 10, "title": "Commitment extraction", "tier": "—", "suppressed": "—",
            "guardrail": "—", "memory": f"STORED({len(found)})", "cost": cl.cost(),
            "calls": len(cl.calls), "final": None, "ok": True}


SCENARIOS = {
    1: scenario_1, 2: scenario_2, 3: scenario_3, 4: scenario_4, 5: scenario_5,
    6: scenario_6, 7: scenario_7, 8: scenario_8, 9: scenario_9, 10: scenario_10,
}


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(rows):
    print(f"\n{HR}\nFLOW TEST SUMMARY\n{HR}")
    print(f"{'#':<3}{'Scenario':<32}{'Tier':<6}{'Suppr':<7}{'Guardrail':<12}{'Memory':<11}{'Cost':<9}")
    print(SUB)
    total_cost = 0.0
    total_calls = 0
    for r in rows:
        total_cost += r.get("cost", 0.0)
        total_calls += r.get("calls", 0)
        tier = str(r.get("tier")) if r.get("ok") else "ERR"
        title = _short(r["title"], 30)
        print(f"{r['num']:<3}{title:<32}{tier:<6}{r['suppressed']:<7}"
              f"{r['guardrail']:<12}{r['memory']:<11}{cost_s(r.get('cost', 0.0)):<9}")
    print(SUB)

    # "all floors held" — computed from the safety-critical scenarios, not asserted
    issues = []
    by = {r["num"]: r for r in rows if r.get("ok") and r.get("final") is not None}
    if 3 in by and int(by[3]["final"].final_tier) < 2:
        issues.append("S3 investor not surfaced")
    if 6 in by and int(by[6]["final"].final_tier) < 2:
        issues.append("S6 conflict not surfaced")
    if 7 in by and int(by[7]["final"].final_tier) != 3:
        issues.append("S7 personal not floored to ASK")
    print(f"Total LLM calls: {total_calls} | Total real cost: {cost_s(total_cost)}")
    if issues:
        print("⚠️  GUARDRAILS: a floor did NOT hold → " + "; ".join(issues))
    else:
        print("Guardrails: all floors held across all scenarios.")
    print(HR)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Pipeline observability harness (dry-run).")
    ap.add_argument("--scenario", default="", help="comma-separated scenario numbers, e.g. 5,6,7")
    ap.add_argument("--no-llm", action="store_true", help="structure only; canned model outputs")
    ap.add_argument("--verbose", action="store_true", help="print the memory block + assembled context")
    args = ap.parse_args(argv)

    which = [int(x) for x in args.scenario.split(",") if x.strip()] if args.scenario else list(SCENARIOS)

    print(HR)
    print("PIPELINE FLOW TEST  —  " + ("STRUCTURE ONLY (--no-llm)" if args.no_llm else "REAL LLM"))
    print("dry-run forced · live DB read-only · composes real decision-path functions")
    print(HR)

    settings = dataclasses.replace(load_settings(), mode="dry_run")
    mem = snapshot_db(settings.db_path)
    calllog = CallLog()
    if args.no_llm:
        llm = FakeLLM(calllog)
    else:
        from assistant.llm.client import LLMClient
        llm = LLMClient(settings, metrics_sink=calllog.sink)
    env = {"mem": mem, "settings": settings, "llm": llm, "calllog": calllog, "verbose": args.verbose}

    rows = []
    for n in which:
        fn = SCENARIOS.get(n)
        if fn is None:
            print(f"\n(no scenario {n})")
            continue
        try:
            rows.append(fn(env))
        except Exception as exc:  # noqa: BLE001 - never crash the whole run
            print(f"\n⚠️  SCENARIO {n} FAILED: {exc}")
            traceback.print_exc()
            rows.append({"num": n, "title": f"scenario {n}", "tier": "ERR", "suppressed": "—",
                         "guardrail": "—", "memory": "—", "cost": 0.0, "calls": 0,
                         "final": None, "ok": False})

    if len(rows) > 1:
        print_summary(rows)
    mem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
