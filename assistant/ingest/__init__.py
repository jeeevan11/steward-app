"""Channel ingestion layer.

Turns a mail channel (Gmail in Phase 1) into the channel-agnostic `Message`/
`Thread` objects the brain reasons over, and performs the channel-side effects
(archive, label, send, undo) behind the `MailSource` interface so the rest of
the system never imports a Google client directly.
"""
