"""Most rings wins, and 6-6 goes to a sudden-death tie-break (2026-11-22).

Viewers called 6-6 endings unfair: the old rule gave the win to whoever broke the
12th ring, so level and even losing ring counts "won"."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import director  # noqa: E402
import quality  # noqa: E402
import render  # noqa: E402
import sim  # noqa: E402

TIE_SEED = 1063          # director.pick(1): 6-6, then a 3.6 s sudden death -> 6-7


def _last_ring_loses_seed():
    """A seed with no tie where the ball that broke the 12th ring still loses (e.g. 7-5).

    Found at run time, not hard-coded: the physics is chaotic, so the same seed can end
    7-5 on Windows and 6-6 on GitHub's Linux runners (seed 3 did, 2026-11-22)."""
    for seed in range(1, 400):
        m = sim.simulate(seed, record=False)
        if m.winner < 0 or sim.tie_time(m) is not None:
            continue
        breakers = [e.ball for e in m.events if e.kind == "break"]
        if breakers and breakers[-1] != m.winner:
            return seed
    raise AssertionError("no last-ring-loses match in seeds 1-399")


LAST_RING_LOSES = _last_ring_loses_seed()


class TieBreakTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tie = sim.simulate(TIE_SEED)

    def test_six_all_goes_to_sudden_death(self):
        m = self.tie
        tie_t = sim.tie_time(m)
        self.assertIsNotNone(tie_t)
        main = [e for e in m.events if e.kind == "break" and e.t <= tie_t]
        self.assertEqual(len(main), sim.N_RINGS)
        self.assertEqual([e.ball for e in main].count(0), [e.ball for e in main].count(1))
        last = [e for e in m.events if e.kind == "break"][-1]
        self.assertEqual(last.ring, sim.N_RINGS)                   # the gold ring
        self.assertEqual(last.ball, m.winner)
        self.assertEqual(sorted(m.breaks), [sim.N_RINGS // 2, sim.N_RINGS // 2 + 1])

    def test_balls_wait_in_the_centre_during_the_pause(self):
        m = self.tie
        tie_t = sim.tie_time(m)
        f0 = int(tie_t * sim.FPS) + 2
        f1 = int((tie_t + sim.TIE_PAUSE) * sim.FPS) - 2
        self.assertEqual(m.frames[f0][2], m.frames[f1][2])
        for x, y in m.frames[f0][2]:
            self.assertLess(abs(x - sim.CX), 60)
            self.assertLess(abs(y - sim.CY), 40)

    def test_most_rings_wins_even_if_the_other_ball_breaks_the_last_ring(self):
        m = sim.simulate(LAST_RING_LOSES, record=False)
        self.assertIsNone(sim.tie_time(m))
        breakers = [e.ball for e in m.events if e.kind == "break"]
        self.assertNotEqual(breakers[-1], m.winner)
        self.assertGreater(m.breaks[m.winner], m.breaks[1 - m.winner])

    def test_director_only_posts_a_tiebreak_that_is_neither_instant_nor_dragging(self):
        d = sim.drama(self.tie)
        self.assertTrue(director.TB_MIN <= d["tiebreak"] <= director.TB_MAX)
        self.assertTrue(director.acceptable(d))
        self.assertFalse(director.acceptable(dict(d, tiebreak=director.TB_MAX + 1)))
        self.assertFalse(director.acceptable(dict(d, tiebreak=director.TB_MIN - 0.5)))

    def test_no_callout_on_the_tying_break(self):
        names = ["A", "B"]
        out = render.score_callouts(self.tie.events, names)
        tie_t = sim.tie_time(self.tie)
        self.assertTrue(all(t < tie_t for t, _, _ in out))

    def test_royale_keeps_its_rules(self):
        m = sim.simulate(5, record=False, **sim.ROYALE)
        self.assertEqual(len(m.rings), sim.ROYALE["n_rings"])
        self.assertIsNone(sim.tie_time(m))


class UploadBlockTests(unittest.TestCase):
    """quality.check_result: a level match never uploads without the tie-break (Max, 2026-11-22)."""

    def test_real_matches_pass(self):
        self.assertEqual(quality.check_result(sim.simulate(TIE_SEED)), [])
        self.assertEqual(quality.check_result(sim.simulate(LAST_RING_LOSES, record=False)), [])

    def test_director_picks_always_pass(self):
        for base in range(1, 13):
            seed, _ = director.pick(base)
            self.assertEqual(quality.check_result(sim.simulate(seed, record=False)), [], seed)

    def test_six_all_without_tiebreak_is_blocked(self):
        m = sim.simulate(TIE_SEED)
        tie_t = sim.tie_time(m)
        m.events = [e for e in m.events if e.kind != "tie" and not (e.kind == "break" and e.t > tie_t)]
        self.assertTrue(any("without the sudden-death tie-break" in f for f in quality.check_result(m)))

    def test_gold_ring_missing_from_the_video_is_blocked(self):
        m = sim.simulate(TIE_SEED)
        m.frames = [(an, al[:sim.N_RINGS], b) for an, al, b in m.frames]
        self.assertTrue(any("never appears" in f for f in quality.check_result(m)))

    def test_old_rule_winner_with_fewer_rings_is_blocked(self):
        m = sim.simulate(LAST_RING_LOSES, record=False)
        m.winner = 1 - m.winner
        self.assertTrue(any("fewer rings" in f for f in quality.check_result(m)))


if __name__ == "__main__":
    unittest.main()
