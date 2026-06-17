"""The action layer: turn brain decisions into real, reversible-or-approved effects.

This package is the only place that mutates the outside world (Gmail) or queues an
item for human approval. It is deliberately decoupled from the control layer
(Telegram): the dispatcher receives a `mail` source and a `notifier` by dependency
injection so there is no import cycle between action/ and control/.

Modules:
  voice        — assemble the voice prefix / build the global voice profile.
  drafting     — draft a reply in your voice (LLM, fail-safe holding draft).
  gmail_actions— send / silent actions / undo, all dry-run aware and guarded.
  dispatcher   — route a FinalDecision to the right tier-specific effect.
"""
