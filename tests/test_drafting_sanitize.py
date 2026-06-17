import unittest

from assistant.action.drafting import strip_dashes


class TestStripDashes(unittest.TestCase):
    def test_em_dash_becomes_comma(self):
        self.assertEqual(strip_dashes("I'll call you — maybe at 5"), "I'll call you, maybe at 5")

    def test_em_dash_no_spaces(self):
        self.assertEqual(strip_dashes("yes—definitely"), "yes, definitely")

    def test_en_dash_range_becomes_hyphen(self):
        self.assertEqual(strip_dashes("5–10 minutes"), "5-10 minutes")

    def test_no_dashes_unchanged(self):
        s = "Hi John,\n\nSounds good, talk soon.\n\nJatin"
        self.assertEqual(strip_dashes(s), s)

    def test_dash_before_newline_not_left_dangling(self):
        self.assertNotIn("—", strip_dashes("Thanks —\nJatin"))
        self.assertNotIn(", \n", strip_dashes("Thanks —\nJatin"))


if __name__ == "__main__":
    unittest.main()
