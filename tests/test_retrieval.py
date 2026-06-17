import unittest

from assistant.memory import retrieval
from assistant.storage import db
from assistant.storage import repositories as repo
from tests.helpers import make_contact, make_thread


class TestRetrieval(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_rules_scoped_most_specific_first(self):
        repo.add_rule(self.conn, scope="global", instruction="be terse")
        repo.add_rule(self.conn, scope="category", match_key="newsletter",
                      instruction="archive newsletters", action="archive")
        repo.add_rule(self.conn, scope="contact", match_key="a@x.com",
                      instruction="always reply to Alex same day")
        contact = make_contact(email="a@x.com")
        ctx = retrieval.get_context(self.conn, make_thread(), contact, category="newsletter")
        self.assertEqual(len(ctx.rules), 3)
        self.assertIn("Alex", ctx.rules[0])           # contact rule first
        self.assertIn("newsletter", ctx.rules[1].lower())  # category second
        self.assertIn("terse", ctx.rules[2])          # global last

    def test_proposed_rules_excluded(self):
        repo.add_rule(self.conn, scope="global", instruction="inferred guess",
                      status="proposed", source="inferred")
        ctx = retrieval.get_context(self.conn, make_thread(), make_contact())
        self.assertEqual(ctx.rules, [])

    def test_commitments_parsed_from_notes(self):
        contact = make_contact(notes="They run product.\nCOMMIT: send the deck\nCOMMIT: intro to Sam")
        ctx = retrieval.get_context(self.conn, make_thread(), contact)
        self.assertEqual(ctx.commitments, ["send the deck", "intro to Sam"])

    def test_render_includes_sender_and_rules(self):
        repo.add_rule(self.conn, scope="global", instruction="be brief")
        contact = make_contact(email="a@x.com", name="Alex", importance=80, flags={"vip"})
        ctx = retrieval.get_context(self.conn, make_thread(), contact)
        text = ctx.render_for_prompt()
        self.assertIn("Alex", text)
        self.assertIn("vip", text)
        self.assertIn("be brief", text)


if __name__ == "__main__":
    unittest.main()
