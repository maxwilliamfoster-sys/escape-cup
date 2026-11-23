"""Home-nations field (tracker test escape-home-nations), built 2026-11-23, OFF until switched on.

Evidence: since 3 Nov, matches with a home nation got a median 3,548 views (n=11) vs 928 for the
rest (n=43). From the cup set in tournament.HOME_FIELD_FROM_EDITION onwards, Wales replaces
Senegal (lowest median, 763, n=4) and England, Scotland, Wales and Ireland are drawn into four
different quarters. Earlier cups must draw exactly as before, or rebuild() breaks the bracket.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tournament  # noqa: E402

HOME = {"gb-eng", "gb-sct", "gb-wls", "ie"}


def field(state):
    return [c for m in state["rounds"][0] for c in (m["a"], m["b"])]


class HomeField(unittest.TestCase):
    def test_off_by_default_so_every_cup_draws_as_before(self):
        self.assertIsNone(tournament.HOME_FIELD_FROM_EDITION)
        self.assertNotIn("gb-wls", field(tournament.new_edition(7)))

    def test_earlier_cups_are_unchanged_when_switched_on(self):
        before = {e: field(tournament.new_edition(e)) for e in range(1, 8)}
        with mock.patch.object(tournament, "HOME_FIELD_FROM_EDITION", 8):
            for e in range(1, 8):
                self.assertEqual(field(tournament.new_edition(e)), before[e], e)

    def test_from_the_switch_cup_all_four_home_nations_in_different_quarters(self):
        with mock.patch.object(tournament, "HOME_FIELD_FROM_EDITION", 7):
            for e in (7, 8, 9, 15):
                codes = field(tournament.new_edition(e))
                self.assertEqual(len(codes), 32)
                self.assertEqual(len(set(codes)), 32)
                self.assertNotIn("sn", codes)
                self.assertTrue(HOME <= set(codes))
                quarters = [next(q for q in range(4) if c in codes[q * 8:(q + 1) * 8]) for c in HOME]
                self.assertEqual(sorted(quarters), [0, 1, 2, 3], e)

    def test_the_bracket_still_replays_from_the_log(self):
        state = tournament.load()
        with mock.patch.object(tournament, "HOME_FIELD_FROM_EDITION", state["edition"] + 1):
            again = tournament.rebuild(state["log"])
        self.assertEqual(field(again), field(state))


if __name__ == "__main__":
    unittest.main()
