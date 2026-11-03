"""Visible standings and a champion (research report action 8). Off by default (STANDINGS_TEST)
until the 2 Nov results are read on 9 Nov. Every number comes from the tournament state."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402
import teams  # noqa: E402
import tournament  # noqa: E402

CODES = [c for c, _, _ in teams.TEAMS]
A, B, C = CODES[:3]
NAME = {c: teams.name(c).upper() for c in CODES}


def state(titles=None, log=(), edition=3):
    s = tournament.new_edition(edition, titles or {})
    s["log"] = list(log)
    return s


class TitleRace(unittest.TestCase):
    def test_most_titles_first(self):
        self.assertEqual(tournament.title_race(state({A: 1, B: 2})), f"TITLES: {NAME[B]} 2  ·  {NAME[A]} 1")

    def test_only_the_top_three(self):
        line = tournament.title_race(state({A: 4, B: 3, C: 2, CODES[3]: 1}))
        self.assertNotIn(NAME[CODES[3]], line)

    def test_a_winner_just_crowned_counts(self):
        self.assertEqual(tournament.title_race(state({A: 1, B: 2}), new_champion=A), f"TITLES: {NAME[A]} 2  ·  {NAME[B]} 2")

    def test_no_titles_yet_says_so(self):
        self.assertEqual(tournament.title_race(state()), "FIRST TITLE UP FOR GRABS")


class Reigning(unittest.TestCase):
    def test_the_last_final_winner_is_champion(self):
        log = [{"edition": 1, "r": 4, "m": 0, "winner": A}, {"edition": 2, "r": 3, "m": 0, "winner": C},
               {"edition": 2, "r": 4, "m": 0, "winner": B}, {"edition": 3, "r": 0, "m": 0, "winner": A}]
        self.assertEqual(tournament.reigning(state(log=log)), B)

    def test_royales_are_not_finals(self):
        self.assertIsNone(tournament.reigning(state(log=[{"series": "royale", "heat": 1, "r": 4, "winner": A}])))

    def test_before_any_final_there_is_no_champion(self):
        self.assertIsNone(tournament.reigning(state()))


class Road(unittest.TestCase):
    def test_wins_left_after_winning_this_round(self):
        self.assertEqual(tournament.road_line(16), "4 WINS FROM THE TROPHY")
        self.assertEqual(tournament.road_line(8), "3 WINS FROM THE TROPHY")
        self.assertEqual(tournament.road_line(2), "1 WIN FROM THE TROPHY")

    def test_the_final_has_no_road_left(self):
        self.assertIsNone(tournament.road_line(1))


class Info(unittest.TestCase):
    def _on(self, s, size, a, b, winner):
        with mock.patch.object(main, "STANDINGS_TEST", True):
            return main.standings_info(s, size, a, b, winner)

    def test_off_means_nothing_is_added(self):
        self.assertFalse(main.STANDINGS_TEST)
        self.assertEqual(main.standings_info(state({A: 1}), 16, A, B, A), {})

    def test_during_a_match_the_titles_table_is_on_screen(self):
        info = self._on(state({A: 1, B: 2}), 16, C, CODES[4], C)
        self.assertEqual(info["standing"], f"TITLES: {NAME[B]} 2  ·  {NAME[A]} 1")
        self.assertEqual(info["road"], "4 WINS FROM THE TROPHY")

    def test_the_champion_is_called_out_when_playing(self):
        log = [{"edition": 2, "r": 4, "m": 0, "winner": A}]
        info = self._on(state({A: 1}, log), 16, A, B, B)
        self.assertEqual(info["standing"], f"{NAME[A]} ARE CHAMPIONS  ·  TITLES: {NAME[A]} 1")

    def test_the_final_card_shows_the_table_with_the_new_champion(self):
        info = self._on(state({A: 1, B: 2}), 1, A, C, A)
        self.assertEqual(info["road"], f"TITLES: {NAME[A]} 2  ·  {NAME[B]} 2")


class Layout(unittest.TestCase):
    def test_a_long_line_still_fits_the_draw_width(self):
        import render
        self.assertLess(render.STANDING_Y, 1920 - 370)   # above TikTok's bottom buttons and caption
        self.assertGreater(render.STANDING_Y, render.sim.CY + render.sim.OUTER_R + 62)   # below "LAST RING"


if __name__ == "__main__":
    unittest.main()
