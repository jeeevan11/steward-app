"""Local web console — a SECOND, read+approve front-end over the same core.

It does NOT replace Telegram and it reimplements NOTHING: every write goes through
the exact guarded functions the Telegram bot calls (repositories.mark_approved /
begin_send / set_pending_draft / mark_skipped, action.gmail_actions.execute_send,
learning.recorder.*, learning.updater.maybe_propose_rule). Reads use the read-only
storage/read_queries.py. The core never imports this package.

Run the API: `python -m assistant.web.api` (binds 127.0.0.1:8000).
Run the UI:  cd assistant/web/frontend && npm install && npm run dev  (127.0.0.1:5173)
See docs/WEB.md for the full seam audit trail.
"""
