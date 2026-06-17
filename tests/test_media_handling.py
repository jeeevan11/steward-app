"""GAP 8 — WhatsApp media: audio transcription, image description, fallbacks."""

from __future__ import annotations

import unittest

from assistant.config import Settings
from assistant.ingest import whatsapp_source as ws
from assistant.storage import db
from assistant.storage import whatsapp_inbox as inbox


class FakeLLM:
    def __init__(self, *, transcript=None, description=None, raise_audio=False):
        self._transcript = transcript
        self._description = description
        self._raise_audio = raise_audio

    def transcribe_audio(self, audio_b64, audio_format="ogg", model=None):
        if self._raise_audio:
            from assistant.llm.client import LLMError
            raise LLMError("boom")
        return self._transcript

    def describe_image(self, image_b64, **kw):
        return self._description


def _mkdb():
    conn = db.open_db(":memory:")
    inbox.ensure(conn)
    return conn


def _put(conn, fields):
    mid = fields["message_id"]
    inbox.put(conn, mid, fields, status="new")
    return inbox.get(conn, mid)


class TestAudio(unittest.TestCase):
    def test_audio_b64_payload_transcribed(self):
        conn = _mkdb()
        row = _put(conn, {"message_id": "wa_1", "jid": "j@s.whatsapp.net",
                          "media_type": "audio", "media_b64": "AAAA", "audio_format": "ogg"})
        src = ws.WhatsAppSource(conn, Settings(), llm=FakeLLM(transcript="hello there"))
        msg = src._materialize(row)
        self.assertIn("hello there", msg.body_text)

    def test_audio_spec_field_audio_b64(self):
        conn = _mkdb()
        # Spec-shaped payload using audio_b64 (no media_type) → normalized to audio.
        row = _put(conn, {"message_id": "wa_2", "jid": "j@s.whatsapp.net",
                          "audio_b64": "AAAA", "audio_format": "ogg"})
        self.assertEqual(row["media_type"], "audio")
        src = ws.WhatsAppSource(conn, Settings(), llm=FakeLLM(transcript="from spec field"))
        msg = src._materialize(row)
        self.assertIn("from spec field", msg.body_text)

    def test_audio_failure_fallback(self):
        conn = _mkdb()
        row = _put(conn, {"message_id": "wa_3", "jid": "j@s.whatsapp.net",
                          "media_type": "audio", "media_b64": "AAAA", "audio_format": "ogg"})
        src = ws.WhatsAppSource(conn, Settings(), llm=FakeLLM(raise_audio=True))
        msg = src._materialize(row)
        self.assertIn("could not transcribe", msg.body_text)


class TestImage(unittest.TestCase):
    def test_image_b64_payload_described(self):
        conn = _mkdb()
        row = _put(conn, {"message_id": "wa_4", "jid": "j@s.whatsapp.net",
                          "media_type": "image", "media_b64": "BBBB"})
        src = ws.WhatsAppSource(conn, Settings(), llm=FakeLLM(description="a cat on a couch"))
        msg = src._materialize(row)
        self.assertIn("a cat on a couch", msg.body_text)

    def test_image_spec_field_image_b64(self):
        conn = _mkdb()
        row = _put(conn, {"message_id": "wa_5", "jid": "j@s.whatsapp.net", "image_b64": "BBBB"})
        self.assertEqual(row["media_type"], "image")
        src = ws.WhatsAppSource(conn, Settings(), llm=FakeLLM(description="a sunset"))
        msg = src._materialize(row)
        self.assertIn("a sunset", msg.body_text)

    def test_image_failure_fallback(self):
        conn = _mkdb()
        row = _put(conn, {"message_id": "wa_6", "jid": "j@s.whatsapp.net",
                          "media_type": "image", "media_b64": "BBBB"})
        src = ws.WhatsAppSource(conn, Settings(), llm=FakeLLM(description=None))
        msg = src._materialize(row)
        self.assertIn("[image]", msg.body_text)


class TestBodyFor(unittest.TestCase):
    def test_audio_missing_b64_fallback(self):
        # No media_b64 → placeholder body.
        body = ws._body_for({"media_type": "audio"}, None)
        self.assertIn("could not transcribe", body)

    def test_image_fallback(self):
        body = ws._body_for({"media_type": "image"}, None)
        self.assertEqual(body, "[image]")

    def test_sticker_empty_body_silent(self):
        # An unsupported/sticker type with no body → empty (silent).
        body = ws._body_for({"media_type": "sticker", "body": ""}, None)
        self.assertEqual(body, "")


if __name__ == "__main__":
    unittest.main()
