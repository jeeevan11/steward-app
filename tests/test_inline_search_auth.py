"""Regression tests for approval-telegram-3.

Inline search must be OWNER-ONLY. Before this fix, ``handle_inline_query`` answered
any Telegram user's inline query with the owner's private Gmail/WhatsApp content — any
stranger who could reach the bot could type "@bot find <anything>" and read it.

These tests inject a fake Telegram ``update``/``context`` (no network, no real bot) and
assert:
  * a NON-OWNER requester gets an empty answer and search is NEVER invoked, and
  * the OWNER gets the real (mocked) search results.

stdlib-only (asyncio + unittest); the only third-party import is ``telegram`` for the
result objects, which the module already requires.
"""

from __future__ import annotations

import asyncio
import unittest

from assistant.control import inline_search


class _FakeFromUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeInlineQuery:
    def __init__(self, query, user_id):
        self.query = query
        self.from_user = _FakeFromUser(user_id)
        self.answered = []  # list of (results, cache_time)

    async def answer(self, results, cache_time=0):
        self.answered.append((list(results), cache_time))


class _FakeUpdate:
    def __init__(self, query, user_id):
        self.inline_query = _FakeInlineQuery(query, user_id)


class _FakeBotData(dict):
    pass


class _FakeApplication:
    def __init__(self, settings):
        self.bot_data = _FakeBotData(settings=settings)


class _FakeContext:
    def __init__(self, settings):
        self.application = _FakeApplication(settings)


class _Settings:
    def __init__(self, telegram_chat_id):
        self.telegram_chat_id = telegram_chat_id


class TestInlineSearchAuth(unittest.TestCase):
    def setUp(self):
        # Hard-fail the test if search is ever reached for a non-owner.
        self._orig_gmail = inline_search.search_gmail
        self._orig_wa = inline_search.search_wa
        self.search_calls = []

        def _tracking_gmail(query, svc):
            self.search_calls.append(("gmail", query))
            return [{"id": "g1", "subject": "Secret term sheet", "sender": "vip@x.com",
                     "date_str": "", "snippet": "private"}]

        def _tracking_wa(query, db):
            self.search_calls.append(("wa", query))
            return [{"id": "w1", "jid": "j@s.whatsapp.net", "sender_name": "Rajesh",
                     "text": "private wa", "ts": 0}]

        inline_search.search_gmail = _tracking_gmail
        inline_search.search_wa = _tracking_wa

    def tearDown(self):
        inline_search.search_gmail = self._orig_gmail
        inline_search.search_wa = self._orig_wa

    def _run(self, update, context, owner_id=None):
        asyncio.run(
            inline_search.handle_inline_query(
                update, context, gmail_service=object(), db=None, owner_id=owner_id
            )
        )

    # ── the core security assertions ─────────────────────────────────────────
    def test_non_owner_gets_nothing_and_never_searches(self):
        settings = _Settings(telegram_chat_id="111")  # owner is user 111
        ctx = _FakeContext(settings)
        update = _FakeUpdate("find rajesh term sheet", user_id=999)  # a stranger

        self._run(update, ctx)

        # Search was NEVER called — no private content was read.
        self.assertEqual(self.search_calls, [])
        # The stranger got exactly one answer, and it was empty.
        self.assertEqual(len(update.inline_query.answered), 1)
        results, _ = update.inline_query.answered[0]
        self.assertEqual(results, [])

    def test_owner_gets_real_results(self):
        settings = _Settings(telegram_chat_id="111")
        ctx = _FakeContext(settings)
        update = _FakeUpdate("find rajesh term sheet", user_id=111)  # the owner

        self._run(update, ctx)

        # Both stores were searched for the owner.
        self.assertEqual(sorted(k for k, _ in self.search_calls), ["gmail", "wa"])
        self.assertEqual(len(update.inline_query.answered), 1)
        results, _ = update.inline_query.answered[0]
        self.assertTrue(len(results) >= 1)  # owner sees their content

    def test_owner_id_type_mismatch_still_matches(self):
        """The owner id from settings is a string; the Telegram from_user.id is an int.
        The gate must compare them as strings so a legit owner is not locked out."""
        settings = _Settings(telegram_chat_id="111")
        ctx = _FakeContext(settings)
        update = _FakeUpdate("find x", user_id=111)  # int id vs str setting
        self._run(update, ctx)
        self.assertTrue(self.search_calls)  # owner matched, search ran

    def test_explicit_owner_id_override_is_honored(self):
        # No settings at all → must fall back to the explicit owner_id and still gate.
        ctx = _FakeContext(settings=None)
        owner = _FakeUpdate("find x", user_id=42)
        self._run(owner, ctx, owner_id=42)
        self.assertTrue(self.search_calls)  # matches explicit owner

        self.search_calls.clear()
        stranger = _FakeUpdate("find x", user_id=43)
        self._run(stranger, ctx, owner_id=42)
        self.assertEqual(self.search_calls, [])  # non-owner blocked
        self.assertEqual(stranger.inline_query.answered[0][0], [])

    def test_fails_closed_when_no_owner_configured(self):
        """If no owner is configured anywhere, deny — never leak to an unauthenticated
        requester just because config is missing."""
        ctx = _FakeContext(settings=_Settings(telegram_chat_id=""))
        update = _FakeUpdate("find x", user_id=111)
        self._run(update, ctx)
        self.assertEqual(self.search_calls, [])
        self.assertEqual(update.inline_query.answered[0][0], [])

    def test_missing_from_user_is_denied(self):
        settings = _Settings(telegram_chat_id="111")
        ctx = _FakeContext(settings)
        update = _FakeUpdate("find x", user_id=111)
        update.inline_query.from_user = None  # malformed update
        self._run(update, ctx)
        self.assertEqual(self.search_calls, [])
        self.assertEqual(update.inline_query.answered[0][0], [])


if __name__ == "__main__":
    unittest.main()
