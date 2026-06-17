"""Natural-language command parsing & dispatch.

You can talk to the assistant in plain English ("pause until tomorrow", "what's
up?", "undo that", "treat anything from Acme as high importance"). This module
asks the LLM to map that free text onto a small, closed set of structured
commands, then applies them against storage / the action layer.

Robustness contract: this NEVER raises. A parse failure, an unknown command, a
malformed JSON blob, or an LLM outage all degrade to a friendly "I didn't
understand that" string. The worst outcome of a misunderstood command is a
no-op, never a crash and never an unintended autonomous action.

Supported commands (the closed set the prompt is asked to emit):
    pause          → set paused=True
    resume         → set paused=False
    status         → return a one-line status summary
    brief          → generate a brief (arg: kind = morning|evening)
    undo           → undo the last reversible action
    decline_all    → skip every open pending item (+ learning)
    set_rule       → add a standing rule (args: scope, match_key, instruction, action)
    set_importance → set a contact's importance (args: email, importance)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from assistant.config import Settings
from assistant.control import briefs
from assistant.llm.client import LLMClient, LLMError
from assistant.llm import prompts
from assistant.logging_setup import get_logger
from assistant.storage import repositories as repo

# action / learning are one-directional imports (control → action/learning).
from assistant.action import gmail_actions
from assistant.learning import recorder

log = get_logger("commands")

_DIDNT_UNDERSTAND = (
    "I didn't understand that. Try: pause, resume, status, brief, undo, or "
    "decline all."
)


def _record_pause(conn: sqlite3.Connection, paused: bool) -> None:
    """Best-effort learning event for a pause/resume. The recorder's exact
    pause API is owned by another module; we degrade gracefully if it differs."""
    try:
        recorder.record_pause(conn, paused=paused)
    except Exception:  # noqa: BLE001 - learning is non-critical; never block a command
        log.debug("record_pause unavailable/failed; skipping learning event")


def _record_skip(conn: sqlite3.Connection, action_row) -> None:
    """Best-effort learning event for a skipped pending item."""
    try:
        recorder.record_skip(conn, action_row)
    except Exception:  # noqa: BLE001 - learning is non-critical
        log.debug("record_skip unavailable/failed; skipping learning event")


def _parse_command_json(raw: str) -> dict[str, Any]:
    """Pull a command dict out of the model's text. Tolerates code fences and
    leading/trailing prose by extracting the first {...} object. Returns {} on
    any failure."""
    if not raw:
        return {}
    text = raw.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _status_summary(conn: sqlite3.Connection, settings: Settings) -> str:
    paused = repo.is_paused(conn)
    pending = repo.open_pending(conn)
    mode = "DRY-RUN" if settings.dry_run else "LIVE"
    state = "paused" if paused else "running"
    return f"Status: {state} · mode {mode} · {len(pending)} item(s) awaiting you."


def _apply(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLMClient,
    mail: Any,
    notifier: Any,
    cmd: dict[str, Any],
) -> str:
    """Apply a parsed command dict. Returns a one-line confirmation."""
    name = str(cmd.get("command", "")).strip().lower()
    # LLM returns a flat dict (scope/instruction/etc at top level).
    # Fall back to cmd itself so set_rule/set_importance fields are always reachable.
    args = cmd.get("args") if isinstance(cmd.get("args"), dict) else cmd

    if name == "pause":
        repo.set_paused(conn, True)
        _record_pause(conn, True)
        return "Paused. I won't act until you resume."

    if name == "resume":
        repo.set_paused(conn, False)
        _record_pause(conn, False)
        return "Resumed. Back to watching your inbox."

    if name == "status":
        return _status_summary(conn, settings)

    if name == "brief":
        kind = str(args.get("kind", "morning")).strip().lower()
        if kind not in ("morning", "evening"):
            kind = "morning"
        return briefs.generate_brief(conn, settings, llm, kind)

    if name == "undo":
        return gmail_actions.undo_last(conn, mail, settings)

    if name == "decline_all":
        open_items = repo.open_pending(conn)
        n = 0
        for row in open_items:
            if repo.mark_skipped(conn, row["id"]):
                _record_skip(conn, row)
                n += 1
        return f"Declined {n} pending item(s)."

    if name == "set_rule":
        scope = str(args.get("scope", "global")).strip().lower() or "global"
        if scope not in ("global", "contact", "category"):
            scope = "global"
        instruction = str(args.get("instruction", "")).strip()
        if not instruction:
            return "I need an instruction for that rule."
        match_key = str(args.get("match_key", "")).strip()
        action = str(args.get("action", "")).strip()
        repo.add_rule(
            conn,
            scope=scope,
            instruction=instruction,
            match_key=match_key,
            action=action,
            source="user",
        )
        where = f" for {match_key}" if match_key else ""
        return f"Got it — new {scope} rule{where}."

    if name == "set_importance":
        email = str(args.get("email", "")).strip().lower()
        if not email:
            return "Which contact? I need their email to set importance."
        try:
            importance = int(args.get("importance", 0))
        except (TypeError, ValueError):
            importance = 0
        importance = max(0, min(100, importance))
        contact = repo.get_or_default_contact(conn, email)
        contact.importance = importance
        repo.upsert_contact(conn, contact)
        return f"Set {email} importance to {importance}/100."

    return _DIDNT_UNDERSTAND


def apply_command(
    conn: sqlite3.Connection,
    settings: Settings,
    llm: LLMClient,
    mail: Any,
    notifier: Any,
    text: str,
) -> str:
    """Parse free text into a command and apply it. Returns a confirmation string.

    Never raises — any failure becomes a friendly fallback message.
    """
    text = (text or "").strip()
    if not text:
        return _DIDNT_UNDERSTAND

    # Ask the model to map the free text to a structured command.
    try:
        system_prefix = prompts.load("command_parse", settings.prompts_dir)
        raw = llm.complete_text(
            system_prefix=system_prefix,
            user_prompt=text,
            use_opus=False,  # cheap pass; the mapping is simple/closed-set
        )
    except FileNotFoundError:
        log.warning("command_parse prompt missing")
        return _DIDNT_UNDERSTAND
    except LLMError as exc:
        log.warning("command parse LLM failed: %s", exc)
        return "I couldn't reach my brain just now — try again in a moment."

    cmd = _parse_command_json(raw)
    if not cmd or "command" not in cmd:
        return _DIDNT_UNDERSTAND

    # If the LLM forgot to fill in the instruction, fall back to the user's own words.
    if cmd.get("command") == "set_rule":
        # _apply reads from cmd["args"] sub-dict OR directly from cmd — patch both.
        if not str(cmd.get("instruction", "")).strip():
            cmd["instruction"] = text
        if isinstance(cmd.get("args"), dict) and not str(cmd["args"].get("instruction", "")).strip():
            cmd["args"]["instruction"] = text

    try:
        return _apply(conn, settings, llm, mail, notifier, cmd)
    except Exception as exc:  # noqa: BLE001 - defensive: a command must never crash the bot
        log.exception("command application failed: %s", exc)
        return "Something went wrong applying that — I've logged it."
