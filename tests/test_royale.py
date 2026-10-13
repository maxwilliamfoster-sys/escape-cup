"""
Cup vs Escape Royale A/B wiring (2026-10-13). Offline: no Buffer, no Telegram, no render.

    py -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402
import quality  # noqa: E402
import teams  # noqa: E402
import tournament  # noqa: E402


def _cup_entries(n):
    """The first n bracket matches of Escape Cup 1, as log entries (team a always wins)."""
    state, out = tournament.rebuild([]), []
    for i in range(n):
        r, m = tournament.next_match(state)
        match = state["rounds"][r][m]
        e = {"edition": 1, "r": r, "m": m, "a": match["a"], "b": match["b"], "winner": match["a"],
             "seed": i, "theme": "neon", "due": f"2026-10-0{1 + i}T18:30:00+00:00",
             "buffer_id": f"cup{i}", "status": "published"}
        out.append(e)
        state = tournament.rebuild(out)
    return out


def _royale_entry(heat, status="published", buffer_id=None):
    return {"series": "royale", "heat": heat, "edition": 1, "codes": main.royale_field(heat),
            "winner": "gb-eng", "podium": ["gb-eng", "br", "jp"], "seed": 1000 + heat,
            "theme": "ocean", "due": "2026-10-13T15:30:00+00:00",
            "buffer_id": buffer_id or f"roy{heat}", "status": status}


class BracketTests(unittest.TestCase):
    def test_royale_entries_do_not_touch_the_bracket(self):
        cup = _cup_entries(3)
        mixed = [cup[0], _royale_entry(1), cup[1], _royale_entry(2), cup[2]]
        a, b = tournament.rebuild(cup), tournament.rebuild(mixed)
        self.assertEqual(a["rounds"], b["rounds"])
        self.assertEqual(tournament.next_match(a), tournament.next_match(b))
        self.assertEqual(len(b["log"]), 5)          # heats stay in the log for reconcile

    def test_heat_numbers_count_royale_entries(self):
        state = tournament.rebuild([_royale_entry(1), *_cup_entries(1), _royale_entry(2)])
        self.assertEqual(main.royale_heat(state), 3)


class ArmTests(unittest.TestCase):
    def test_both_arms_every_day_and_slots_swap(self):
        state = tournament.rebuild(_cup_entries(1))        # first post 2026-10-01: ramp over
        days = []
        for d in (13, 14):                                   # Tue, Wed (weekday slots)
            arms = [main.arm_for(datetime(2026, 10, d, h, m, tzinfo=main.LONDON).astimezone(timezone.utc),
                                 state) for h, m in main.WEEKDAY_SLOTS]
            self.assertEqual(sorted(arms), ["cup", "royale"])
            days.append(arms)
        self.assertEqual(days[0], days[1][::-1])


class FieldTests(unittest.TestCase):
    def test_field_shape(self):
        for heat in range(1, 30):
            codes = main.royale_field(heat)
            self.assertEqual(len(codes), main.ROYALE_SIZE)
            self.assertEqual(len(set(codes)), len(codes))
            self.assertIn("gb-eng", codes)
            self.assertEqual(sum(c in teams.HOME_NATIONS for c in codes), 1)
            self.assertFalse(set(codes) & quality.EXCLUDED_CODES)
            self.assertTrue(all(c in teams.BY_CODE for c in codes))
            self.assertEqual(quality.check_royale(codes, codes[0], [5] * 16), [])

    def test_field_is_deterministic(self):
        self.assertEqual(main.royale_field(7), main.royale_field(7))
        self.assertNotEqual(main.royale_field(7), main.royale_field(8))

    def test_captions_pass_the_gate(self):
        import random
        for heat in range(1, 25):
            cap = main.make_royale_caption(main.royale_field(heat), heat, random.Random(heat))
            self.assertEqual(quality.check_caption(cap, None, None, []), [], cap)

    def test_league_points(self):
        state = tournament.rebuild([_royale_entry(1)])      # podium gb-eng, br, jp
        line = main.royale_league(state, ["br", "gb-eng", "fr"])
        self.assertEqual(line, "LEAGUE: ENGLAND 5  ·  BRAZIL 5  ·  JAPAN 1")


class ReconcileTests(unittest.TestCase):
    def test_vetoed_heat_excludes_its_seed_and_rewinds(self):
        cup = _cup_entries(1)
        heat = _royale_entry(1, status="scheduled", buffer_id="gone")
        state = tournament.rebuild(cup + [heat])
        with mock.patch.object(main.publish, "pending_posts", return_value=[]), \
                mock.patch.object(main.publish, "get_post", return_value=None), \
                mock.patch.object(main.notify, "send") as send:
            out = main.reconcile(state, "org", "chan")
        self.assertEqual(len(out["log"]), 1)
        self.assertEqual(out["excluded_seeds"]["royale-1"], [heat["seed"]])
        self.assertIn("Escape Royale heat 1", send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
