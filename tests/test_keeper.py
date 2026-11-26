"""Beat The Keeper club league (keeper.py), sample built 2026-11-26."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keeper as K  # noqa: E402


class KeeperTests(unittest.TestCase):
    def test_twenty_clubs_no_crests_needed(self):
        self.assertEqual(len(K.CLUBS), 20)
        self.assertEqual(len(K.BY_CODE), 20)
        for code, _name, prim, sec, pattern in K.CLUBS:
            self.assertEqual(len(code), 3)
            self.assertIn(pattern, ("solid", "stripes"))
            self.assertTrue(prim.startswith("#") and sec.startswith("#"))

    def test_fixtures_full_double_round_robin(self):
        rounds = K.fixtures()
        self.assertEqual(len(rounds), 38)
        pairs = [p for md in rounds for p in md]
        self.assertEqual(len(pairs), 380)
        self.assertEqual(len(set(pairs)), 380)                    # every home/away pairing once
        for md in rounds:
            teams = [c for p in md for c in p]
            self.assertEqual(len(set(teams)), 20)                 # everyone plays once a matchday

    def test_first_to_three_and_deterministic(self):
        a = K.simulate(7003, "ARS", "TOT", record=False)
        b = K.simulate(7003, "ARS", "TOT", record=False)
        self.assertEqual(a.goals, b.goals)
        self.assertEqual(max(a.goals), K.GOALS_TO_WIN)
        self.assertEqual(a.goals[a.winner], K.GOALS_TO_WIN)
        self.assertEqual(sum(1 for e in a.events if e.kind == "break"), sum(a.goals))

    def test_table_orders_by_points_then_goal_difference(self):
        rows = K.table([("ARS", "TOT", 2, 3), ("LIV", "MUN", 3, 0)])
        self.assertEqual([r["code"] for r in rows[:2]], ["LIV", "TOT"])
        self.assertEqual(rows[0]["pts"], 3)


if __name__ == "__main__":
    unittest.main()
