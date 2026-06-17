"""Stdlib-only unit tests for assistant.web.miniapp_auth.

Covers:
  - Session token round-trip (create + verify)
  - Expired token
  - Tampered token
  - Empty token
  - validate_init_data with invalid hash
  - validate_init_data with old auth_date
  - validate_init_data with a correctly signed payload
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
import urllib.parse

from assistant.web import miniapp_auth

BOT_TOKEN = "test_bot_token_12345"


class TestCreateAndVerifySessionToken(unittest.TestCase):
    """Round-trip: create_session_token -> verify_session_token returns user_id."""

    def test_create_and_verify_session_token(self):
        user_id = "42"
        token = miniapp_auth.create_session_token(user_id, BOT_TOKEN)
        result = miniapp_auth.verify_session_token(token, BOT_TOKEN)
        self.assertEqual(result, user_id)


class TestVerifyExpiredToken(unittest.TestCase):
    """Token created with expiry_seconds=-1 must already be expired."""

    def test_verify_expired_token(self):
        token = miniapp_auth.create_session_token("99", BOT_TOKEN, expiry_seconds=-1)
        result = miniapp_auth.verify_session_token(token, BOT_TOKEN)
        self.assertIsNone(result)


class TestVerifyTamperedToken(unittest.TestCase):
    """Flipping one character in the token must invalidate the signature."""

    def test_verify_tampered_token(self):
        token = miniapp_auth.create_session_token("7", BOT_TOKEN)
        # Flip the last character of the base64 string.
        chars = list(token)
        original = chars[-1]
        chars[-1] = "A" if original != "A" else "B"
        tampered = "".join(chars)
        result = miniapp_auth.verify_session_token(tampered, BOT_TOKEN)
        self.assertIsNone(result)


class TestVerifyEmptyToken(unittest.TestCase):
    """An empty string must not be accepted as a valid token."""

    def test_verify_empty_token(self):
        result = miniapp_auth.verify_session_token("", BOT_TOKEN)
        self.assertIsNone(result)


class TestValidateInitDataInvalidHash(unittest.TestCase):
    """init_data with a wrong hash must be rejected."""

    def test_validate_init_data_invalid_hash(self):
        user = json.dumps({"id": 1, "first_name": "Alice"})
        auth_date = str(int(time.time()))
        data = {
            "user": user,
            "auth_date": auth_date,
            "hash": "deadbeef" * 8,  # clearly wrong hash (64 hex chars)
        }
        init_data_str = urllib.parse.urlencode(data)
        result = miniapp_auth.validate_init_data(init_data_str, BOT_TOKEN)
        self.assertIsNone(result)


class TestValidateInitDataOldAuthDate(unittest.TestCase):
    """init_data whose auth_date is 600 seconds in the past must be rejected
    (the module rejects data older than 300 seconds)."""

    def test_validate_init_data_old_auth_date(self):
        user = json.dumps({"id": 2, "first_name": "Bob"})
        auth_date = str(int(time.time()) - 600)
        data = {"user": user, "auth_date": auth_date}
        # Build a *valid* signature so the only rejection reason is the stale date.
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, check_str.encode(), hashlib.sha256).hexdigest()
        data["hash"] = computed_hash
        init_data_str = urllib.parse.urlencode(data)
        result = miniapp_auth.validate_init_data(init_data_str, BOT_TOKEN)
        self.assertIsNone(result)


class TestValidateInitDataValid(unittest.TestCase):
    """Correctly signed init_data must return a dict with the expected user."""

    def test_validate_init_data_valid(self):
        user = json.dumps({"id": 123, "first_name": "Test"})
        auth_date = str(int(time.time()))
        data = {"user": user, "auth_date": auth_date}
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, check_str.encode(), hashlib.sha256).hexdigest()
        data["hash"] = computed_hash
        init_data_str = urllib.parse.urlencode(data)
        result = miniapp_auth.validate_init_data(init_data_str, BOT_TOKEN)
        self.assertIsNotNone(result)
        self.assertEqual(result["user"]["id"], 123)


if __name__ == "__main__":
    unittest.main()
