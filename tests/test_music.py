"""Background music (music.py) and the in-match engagement lines (render.score_callouts), 2026-11-18."""
import json
import os
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import audio  # noqa: E402
import music  # noqa: E402
import render  # noqa: E402
import sim  # noqa: E402


def brk(t, ball):
    return NS(kind="break", t=t, ball=ball, ring=0, x=540.0, v=0.0)


class CalloutTests(unittest.TestCase):
    def test_level_and_lead_change(self):
        ev = [brk(1, 0), brk(3, 1), brk(5, 1), brk(7, 0), brk(9, 0), brk(11, 0)]
        out = render.score_callouts(ev, ["A", "B"])
        self.assertEqual([(t, s) for t, s, _ in out],
                         [(3, "ALL SQUARE!"), (5, "B TAKE THE LEAD!"), (7, "ALL SQUARE!"),
                          (9, "A TAKE THE LEAD!")])

    def test_winning_break_never_gets_a_callout(self):
        out = render.score_callouts([brk(1, 0), brk(2.5, 1)], ["A", "B"])
        self.assertEqual(out, [])

    def test_callouts_never_flicker(self):
        out = render.score_callouts([brk(1, 0), brk(1.5, 1), brk(2, 0), brk(2.3, 1), brk(9, 0)], ["A", "B"])
        self.assertEqual([t for t, _, _ in out], [1.5])


class MusicMixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.match = sim.simulate(117197228160)
        cls.win = len(cls.match.frames) / sim.FPS
        cls.total = cls.win + render.CARD_SECONDS

    def test_marbles_stay_well_above_the_music(self):
        cap = {}
        orig = audio._music_bed

        def spy(m, L, R, n, wt, bts):
            b = orig(m, L, R, n, wt, bts)
            cap["marbles"], cap["bed"] = (L + R) / 2, b
            return b

        tone = np.sin(2 * np.pi * 330 * np.arange(int(self.total * audio.SR)) / audio.SR)
        audio._music_bed = spy
        try:
            st = audio.build(self.match, self.total, self.win, 1, music=tone / np.sqrt(np.mean(tone ** 2)))
        finally:
            audio._music_bed = orig
        n = int(self.win * audio.SR)
        gap = 20 * np.log10(np.sqrt(np.mean(cap["marbles"][:n] ** 2)) / np.sqrt(np.mean(cap["bed"][:n] ** 2)))
        self.assertGreater(gap, 6.0)                     # music clearly under the marbles
        self.assertTrue(np.all(np.isfinite(st)))
        after = cap["bed"][int((self.win + 0.4) * audio.SR):]
        self.assertLess(np.sqrt(np.mean(after ** 2)), 0.2 * np.sqrt(np.mean(cap["bed"][:n] ** 2)))  # drops at the win

    def test_caption_credits_the_track_that_plays(self):
        import json
        import main
        seed = 117197228160
        c = json.load(open(os.path.join(music.MUSIC_DIR, "credits.json"), encoding="utf-8"))[
            os.path.basename(music.pick(seed))]
        cap = main.with_music_credit("A vs B\nline\ntease\n#a #b #c", seed)
        line = cap.split("\n")[-2]
        self.assertEqual(line, f"Music: \"{c['title']}\" by {c['artist']} on SoundCloud, CC BY 3.0")
        self.assertNotRegex(line, r"https?://|\.com/")   # the upload gate rejects caption links
        self.assertEqual(cap.split("\n")[-1], "#a #b #c")
        self.assertEqual(music.pick(seed), music.pick(seed))

    def test_no_music_is_the_old_mix(self):
        a = audio.build(self.match, self.total, self.win, 1)
        b = audio.build(self.match, self.total, self.win, 1, music=None)
        self.assertTrue(np.array_equal(a, b))

    def test_bed_fails_soft(self):
        self.assertIsNone(music.bed(1, 10.0, "definitely-not-ffmpeg-xyz"))

    def test_clips_and_licences_present(self):
        names = [os.path.basename(p) for p in music.clips()]
        self.assertGreaterEqual(len(names), 10)
        lic = open(os.path.join(music.MUSIC_DIR, "LICENSES.txt"), encoding="utf-8").read()
        credits = json.load(open(os.path.join(music.MUSIC_DIR, "credits.json"), encoding="utf-8"))
        for n in names:
            self.assertIn(n, lic, f"{n} has no licence entry")
            self.assertEqual(credits[n]["licence"], "CC BY 3.0")
            proof = json.load(open(os.path.join(music.MUSIC_DIR, "licences", f"{credits[n]['id']}.json"),
                                   encoding="utf-8"))
            self.assertEqual(proof["license"], "cc-by", f"{n}: saved proof is not CC BY")


if __name__ == "__main__":
    unittest.main()
