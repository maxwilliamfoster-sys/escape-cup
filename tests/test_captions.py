"""
Caption fixes (2026-10-19): unique royale first line per heat, captions checked
before rendering. Offline.

    py -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402
import quality  # noqa: E402
import upload_gate  # noqa: E402


class TestRoyaleCaption(unittest.TestCase):
    def setUp(self):
        self.codes = main.royale_field(7)

    def test_royale_first_line_names_the_heat(self):
        c = main.make_royale_caption(self.codes, 7, random.Random(1))
        first = c.split("\n")[0]
        self.assertIn("heat 7", first.lower())
        self.assertLessEqual(len(first), 110)

    def test_royale_head_skips_recent_variants(self):
        # every phrasing for heat 7 except one is "recent" (re-render after a veto)
        variants = [main.make_royale_caption(self.codes, 7, random.Random(s)).split("\n")[0]
                    for s in range(200)]
        distinct = sorted(set(variants))
        self.assertGreaterEqual(len(distinct), len(main.ROYALE_HEADS))
        c = main.make_royale_caption(self.codes, 7, random.Random(1), recent_heads=distinct[:-1])
        self.assertEqual(c.split("\n")[0], distinct[-1])

    def test_deadlock_regression_old_heads_do_not_block(self):
        recent = [h + "\nx\ny\n#a #b #c" for h in main.ROYALE_HEADS] * 8   # the 10-19 state
        heads = [r.split("\n")[0] for r in recent]
        c = main.make_royale_caption(self.codes, 7, random.Random(3), recent_heads=heads)
        self.assertEqual(quality.check_caption(c, None, None, recent), [])
        self.assertEqual(upload_gate.check_text("tiktok", caption=c, recent_texts=heads)[0], [])

    def test_second_line_does_not_repeat_heat(self):
        c = main.make_royale_caption(self.codes, 7, random.Random(1))
        self.assertNotIn("heat 7", c.split("\n")[1].lower())
        # 2026-10-23: England and the home nation moved to the first line; the
        # second line names other countries in the heat, not the same ones again
        self.assertNotIn("England", c.split("\n")[1])
        self.assertIn("more", c.split("\n")[1])

    def test_first_line_starts_with_countries_heat_at_end(self):
        """Audit 2026-10-21 (MASTER_PLAN #11): countries first (search keywords and a
        stake), 'heat N' moves to the end so every heat's first line is still unique."""
        for heat in range(1, 80):
            codes = main.royale_field(heat)
            c = main.make_royale_caption(codes, heat, random.Random(heat))
            first = c.split("\n")[0]
            self.assertTrue(first.startswith("England, "), first)
            home = next(x for x in codes if x in main.teams.HOME_NATIONS)
            self.assertIn(main.teams.name(home), first.split("?")[0])
            self.assertTrue(first.endswith(f"· heat {heat}"), first)
            self.assertLessEqual(len(first), 110, first)
            self.assertEqual(quality.check_caption(c, None, None, []), [], c)


class TestCaptionFirst(unittest.TestCase):
    def test_pick_caption_rerolls_words(self):
        calls = []

        def build(rng):
            c = f"cap{rng.random():.6f}"
            calls.append(c)
            return c

        def check(c):
            return ["repeat"] if c == calls[0] else []
        cap, fails = main._pick_caption(build, 42, check)
        self.assertEqual(fails, [])
        self.assertEqual(cap, calls[1])

    def test_pick_caption_first_try_keeps_the_seeds_caption(self):
        cap, fails = main._pick_caption(lambda rng: f"cap{rng.random()}", 42, lambda c: [])
        self.assertEqual(cap, f"cap{random.Random(42).random()}")

    def test_caption_failure_skips_render(self):
        phys = SimpleNamespace(winner=0, frames=[0] * 60, breaks=[])
        with mock.patch.object(main.director, "pick_royale", return_value=(123, {})), \
                mock.patch.object(main.sim, "simulate", return_value=phys), \
                mock.patch.object(main.sim, "standings", return_value=[0, 1, 2]), \
                mock.patch.object(main.render, "render_royale") as render_mock, \
                mock.patch.object(main.quality, "check_caption",
                                  return_value=["caption repeats a recent one"]):
            v = main.build_royale({"log": [], "recent_captions": []})
        self.assertTrue(v["caption_only"])
        self.assertTrue(all(f.startswith("caption: ") for f in v["failures"]))
        render_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
