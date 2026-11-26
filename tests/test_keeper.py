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


class KeeperAudioTests(unittest.TestCase):
    """keeper_audio.py (2026-11-26): bounces play the match's chord progression."""

    def test_peg_notes_are_chord_tones_in_phone_range(self):
        import keeper_audio as KA
        for tonic in (60, 62, 63, 65, 67):
            for prog in KA.PROGRESSIONS:
                for ch in prog:
                    tones = KA._tones(ch, tonic)
                    self.assertGreaterEqual(len(tones), 5)             # one per peg row
                    pcs = KA._pcs(ch, tonic)
                    for m in tones:
                        self.assertIn(m % 12, pcs)
                        self.assertTrue(KA.NOTE_LO <= m <= KA.NOTE_HI)

    def test_soft_and_deterministic(self):
        import numpy as np
        import keeper_audio as KA
        m = K.simulate(7003, "ARS", "TOT", record=False)
        a = KA.build(m, m.duration + 2.0, m.duration, m.seed)
        b = KA.build(m, m.duration + 2.0, m.duration, m.seed)
        self.assertTrue(np.array_equal(a, b))
        spec = np.abs(np.fft.rfft(a.mean(axis=1))) ** 2
        f = np.fft.rfftfreq(len(a), 1 / 44100)
        self.assertLess(spec[f > 2000].sum() / spec.sum(), 0.005)       # nothing sharp


class LeagueMusicTests(unittest.TestCase):
    """league_music.py: Max's NCS track ("Recall", Gabriawll), 2026-11-26."""

    def test_every_clip_is_credited_and_keyed(self):
        import league_music as LM
        clips = LM.clips()
        if not clips:                       # beds aren't committed (NCS); CI tests run without them
            self.skipTest("no league music beds here (python tools/make_league_music.py --download)")
        creds = LM._credits()
        for path in clips:
            c = creds[os.path.basename(path)]
            self.assertTrue(c["licence"] and c["credit"] and c["source"].startswith("https://"))
            self.assertTrue(os.path.exists(os.path.join(LM.MUSIC_DIR, "licences")))
            self.assertGreaterEqual(c["key_conf"], 0.65)          # clear key: bounce notes won't clash
            self.assertIn(c["mode"], ("major", "minor"))

    def test_credit_line_and_key_follow_the_pick(self):
        import league_music as LM
        if not LM.clips():
            self.assertEqual(LM.credit(7003), "")                   # no music -> no credit line
            return
        line = LM.credit(7003)
        self.assertIn("Recall", line)
        self.assertIn("NoCopyrightSounds", line)
        self.assertNotIn("http", line)                             # captions reject links
        self.assertEqual(LM.credit(7003), LM.credit(7003))         # pure: caption matches the render
        self.assertTrue(0 <= LM.key(7003) < 12)


class BeatSyncTests(unittest.TestCase):
    """keeper_sync.py (2026-11-26): replay retimed so bounces land on the music's beat."""

    def test_beat_grid_finds_tempo_and_phase(self):
        import numpy as np
        import keeper_sync as S
        sr, bpm, first = 44100, 127.0, 0.21
        x = np.zeros(sr * 12)
        t = np.arange(int(0.12 * sr)) / sr
        kick = np.sin(2 * np.pi * 60 * t) * np.exp(-t / 0.03)
        b = first
        while b < 11.8:
            i = int(b * sr)
            x[i:i + len(kick)] += kick
            b += 60 / bpm
        p, ph = S.beat_grid(x, sr)
        self.assertAlmostEqual(60 / p, bpm, delta=0.5)
        self.assertLess(abs(ph - first), 0.02)
        p2, ph2 = S.beat_grid(x, sr, bpm=bpm)
        self.assertLess(abs(ph2 - first), 0.02)

    def test_retime_keeps_the_match_and_puts_goals_on_beats(self):
        import numpy as np
        import keeper_sync as S
        m = K.simulate(7003, "ARS", "TOT")
        p, ph = 60 / 127.0, 0.35
        r = S.retime(m, p, ph)
        self.assertEqual((r.winner, r.goals), (m.winner, m.goals))          # never rigs the result
        self.assertEqual([e.kind for e in r.events], [e.kind for e in m.events])
        st, vt = S.anchors(m, p, ph)
        sp = np.diff(st) / np.diff(vt)
        self.assertTrue((np.diff(vt) > 0).all())
        self.assertGreaterEqual(sp.min(), S.SPEED_MIN - 1e-6)
        self.assertLessEqual(sp.max(), S.SPEED_MAX + 1e-6)
        goals = [e.t for e in r.events if e.kind == "break"]
        on_beat = [abs(((t - ph) / p - round((t - ph) / p)) * p) < 0.005 for t in goals]
        self.assertGreaterEqual(sum(on_beat), len(goals) - 1)
        self.assertGreater(S.on_grid_share(r, p, ph), 2 * S.on_grid_share(m, p, ph))


if __name__ == "__main__":
    unittest.main()
