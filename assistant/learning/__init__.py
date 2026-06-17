"""The learning layer: turn your decisions into durable signal.

Every time you approve / edit / skip a draft, override the tier, or undo an action,
the `recorder` writes a `learning_event`. The `updater` reads those events back
*conservatively*: a single signal is only a hint, but the same signal repeated for
the same sender or category crosses a threshold and produces a **proposed** rule
(source='inferred', status='proposed'). Proposed rules NEVER act on their own — the
caller surfaces the suggestion and the human confirms it before it goes active.

Stdlib only. Both modules take an open connection so they compose in the caller's
transaction.
"""
