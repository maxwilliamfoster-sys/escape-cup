"""Escape League weekly points league (league.py) and its alternation with the cup (main.py), 2026-11-26."""
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keeper as K  # noqa: E402
import league as L  # noqa: E402


def play_season(log, season_mds=L.MATCHDAYS):
    """Append league entries the way main.run does (featured match from keeper.pick)."""
    days = []
    for _ in range(season_mds):
        season, md = L.position(log)
        h, a = L.featured(season, md, L.season_results(log, season))
        m = K.simulate(L.base_seed(season, md) * 1000, h, a, record=False)
        k = 1
        while m.winner < 0:
            m = K.simulate(L.base_seed(season, md) * 1000 + k, h, a, record=False)
            k += 1
        day = L.matchday(log, (h, a, m.goals[0], m.goals[1]))
        log.append({"series": "league", "season": season, "md": md, "a": h, "b": a,
                    "winner": (h, a)[m.winner], "results": day["results"], "champion": day["champion"]})
        days.append(day)
    return days


class LeagueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.log = []
        cls.days = play_season(cls.log)

    def test_all_twenty_clubs_and_ten_distinct_opponents(self):
        fx = L.season_fixtures(1)
        self.assertEqual(len(fx), 10)
        seen = set()
        for md in fx:
            self.assertEqual(sorted(c for p in md for c in p), sorted(K.BY_CODE))   # everyone plays
            for h, a in md:
                self.assertNotIn(frozenset((h, a)), seen)                          # never twice
                seen.add(frozenset((h, a)))

    def test_points_table_and_champion_after_ten_matchdays(self):
        last = self.days[-1]
        self.assertEqual(last["md"], 10)
        self.assertEqual(len(last["table"]), 20)
        self.assertEqual(sum(r["p"] for r in last["table"]), 200)
        self.assertEqual(sum(r["pts"] for r in last["table"]), 100 * 3)            # 3 per match, no draws
        self.assertEqual(last["champion"], last["table"][0]["code"])
        self.assertTrue(all(d["champion"] is None for d in self.days[:-1]))
        self.assertEqual(L.champions(self.log), [(1, last["champion"], last["table"][0]["pts"])])
        pts = [r["pts"] for r in last["table"]]
        self.assertEqual(pts, sorted(pts, reverse=True))

    def test_tease_matches_the_next_post(self):
        for prev, nxt in zip(self.days, self.days[1:]):
            self.assertEqual(tuple(prev["next_fixture"]), tuple(nxt["results"][0][:2]))
        self.assertEqual(L.position(self.log), (2, 1))                            # season 2 next
        self.assertEqual(tuple(self.days[-1]["next_fixture"]), L.featured(2, 1, []))

    def test_deterministic(self):
        log2 = []
        self.assertEqual([d["results"] for d in play_season(log2, 3)], [d["results"] for d in self.days[:3]])

    def test_every_result_first_to_three(self):
        for d in self.days:
            self.assertEqual(len(d["results"]), 10)
            for h, a, hg, ag in d["results"]:
                self.assertEqual(max(hg, ag), K.GOALS_TO_WIN)
                self.assertLess(min(hg, ag), K.GOALS_TO_WIN)

    def test_hashtags(self):
        self.assertEqual(L.hashtag("NFO"), "#nottmforest")
        self.assertEqual(L.hashtag("MUN"), "#manutd")


class AlternationTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.on = mock.patch.object(main, "league_on", return_value=True)
        self.on.start()

    def tearDown(self):
        self.on.stop()

    def test_series_alternates(self):
        m = self.main
        self.assertEqual(m.next_series({"log": [{"edition": 1}]}), "league")
        self.assertEqual(m.next_series({"log": [{"edition": 1}, {"series": "league"}]}), "cup")
        with mock.patch.object(m, "league_on", return_value=False):
            self.assertEqual(m.next_series({"log": [{"edition": 1}]}), "cup")

    def test_cup_arms_alternate_per_cup_post(self):
        m = self.main
        log, arms = [], []
        for k in range(8):                     # cup, league, cup, league...
            if m.next_series({"log": log}) == "cup":
                arms.append(m._cup_parity({"log": log}))
                log.append({"edition": 1})
            else:
                log.append({"series": "league"})
        self.assertEqual(arms, [0, 1, 0, 1])

    def test_bracket_replay_ignores_league_entries(self):
        import tournament
        st = tournament.rebuild([])
        r, mi = tournament.next_match(st)
        match = st["rounds"][r][mi]
        cup = {"edition": 1, "r": r, "m": mi, "a": match["a"], "b": match["b"], "winner": match["a"], "seed": 1}
        st2 = tournament.rebuild([{"series": "league", "season": 1, "md": 1, "a": "ARS", "b": "TOT"}, cup])
        self.assertEqual(st2["posts"], 1)

    def test_keys_and_labels(self):
        m = self.main
        e = {"series": "league", "season": 2, "md": 7, "a": "LIV", "b": "CHE"}
        self.assertEqual(m.seed_key(e), "league-2-7")
        self.assertIn("Liverpool v Chelsea", m.label(e))

    def test_caption_passes_checks_on_every_matchday(self):
        m = self.main
        log = []
        days = play_season(log)
        for d in days:
            h, a = d["results"][0][:2]
            cap = m.with_league_credit(m.make_league_caption(d, h, a, random.Random(d["md"])), 7003)
            fails = m.quality.check_caption(cap, h, a, [], name=K.name)
            self.assertEqual(fails, [], cap)
            self.assertNotRegex(cap.lower(), "premier")
            self.assertEqual(m.check_league_day(d, h, a, h if d["results"][0][2] == 3 else a), [])


if __name__ == "__main__":
    unittest.main()
