"""Shared constructors for tests. Stdlib only."""

from __future__ import annotations

from assistant.models import (
    Contact,
    Decision,
    Message,
    Reversibility,
    Stakes,
    Thread,
    Tier,
)


def make_message(body: str = "", *, subject: str = "", from_me: bool = False,
                 sender: str = "a@x.com", recipients=None) -> Message:
    return Message(
        id="m1",
        thread_id="t1",
        sender_email="" if from_me else sender,
        subject=subject,
        body_text=body,
        from_me=from_me,
        recipients=recipients or ["me@x.com"],
    )


def make_thread(*messages: Message, subject: str = "Re: hi") -> Thread:
    msgs = list(messages) or [make_message("hello")]
    return Thread(id="t1", subject=subject, messages=msgs)


def make_contact(**kw) -> Contact:
    base = dict(email="a@x.com", name="Alex")
    base.update(kw)
    return Contact(**base)


def make_decision(**kw) -> Decision:
    base = dict(
        category="personal",
        intent="asks a question",
        sender_importance=20,
        stakes=Stakes.MEDIUM,
        reversibility=Reversibility.REVERSIBLE,
        proposed_tier=Tier.FYI,
        confidence=0.9,
        needs_reply=True,
        reasoning="",
        suggested_action="reply",
        one_line_summary="",
    )
    base.update(kw)
    return Decision(**base)
