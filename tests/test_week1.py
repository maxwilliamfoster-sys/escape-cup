"""
Week-1 fixes from the growth-lab audit (2026-10-21; MASTER_PLAN #5, #6, #11).
Offline: no Buffer, no Telegram, no render.

    py -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
from PIL import ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import analytics  # noqa: E402
import audio  # noqa: E402
import growth_stats  # noqa: E402
import main  # noqa: E402
import render  # noqa: E402


def _write(d, obj):
    p = os.path.join(d, "posting.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))
    return p


class PostingConfigTests(unittest.TestCase):
    def test_repo_config_is_seven_slots_without_kill_switch(self):
        with open(os.path.join(ROOT, "posting.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertFalse(cfg["kill_switch"])
        self.assertEqual(main.load_day_slots(), main.DAY_SLOTS)
        self.assertEqual(len(main.DAY_SLOTS), 7)

    def test_kill_switch_drops_to_four_midday_slots(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(ROOT, "posting.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["kill_switch"] = True
            slots = main.load_day_slots(_write(d, cfg))
        self.assertEqual(len(slots), 4)
        self.assertGreaterEqual(min(slots), (11, 0))
        self.assertLessEqual(max(slots), (16, 30))

    def test_broken_config_never_stops_posting(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("{not json", {"kill_switch": False, "day_slots_uk": ["25:99"]},
                        {"kill_switch": True}, {}):
                self.assertEqual(main.load_day_slots(_write(d, bad)), main.DEFAULT_DAY_SLOTS, bad)
            self.assertEqual(main.load_day_slots(os.path.join(d, "missing.json")), main.DEFAULT_DAY_SLOTS)


class CapAndCardTests(unittest.TestCase):
    def test_ai_cap(self):
        self.assertEqual(main.AI_DAILY_CAP, 3)

    def test_shorter_cards_both_arms(self):
        self.assertEqual(render.CARD_SECONDS, 1.5)
        self.assertEqual(render.ROYALE_CARD_SECONDS, 2.0)

    def test_audio_fades_out_instead_of_cutting_the_chime(self):
        """The win chime rings for 2 s; a 1.5 s card used to cut it mid-note (a click)."""
        match = SimpleNamespace(events=[], n_balls=2)
        win = 10.0
        st = audio.build(match, win + render.CARD_SECONDS, win, 1)
        tail = np.abs(st[-int(0.01 * audio.SR):]).max()
        self.assertLess(tail, 0.01 * np.abs(st).max())
        # the chime itself is still there (fade only touches the very end)
        body = np.abs(st[int((win + 0.2) * audio.SR):int((win + 0.9) * audio.SR)]).max()
        self.assertGreater(body, 0.2 * np.abs(st).max())


class ThemeTests(unittest.TestCase):
    @staticmethod
    def _sim(pattern):
        """Append entries (c = cup, r = royale) and return the royale themes in order."""
        state = {"log": [], "posts": 0}
        themes = []
        for i, kind in enumerate(pattern):
            if kind == "r":
                t = main.royale_theme(state)
                themes.append(t)
                state["log"].append({"series": "royale", "theme": t})
            else:
                t = main.cup_theme(state)
                state["log"].append({"theme": t})
                state["posts"] += 1
        return state, themes

    def test_no_two_consecutive_posts_share_a_theme(self):
        # live 2026-10-24: royale "neon" at 10:00Z then cup "neon" at 11:30Z (the royale's
        # theme comes from len(log), the cup's from the cup count, so they could meet)
        for pattern in ("crcrcrcrcrcrcrcrcrcrcrcr", "crcrcrc" + "rcrcrcr" * 4, "crrcccrrrcrcccccrr",
                        "rcrcrcrcrc" * 3, "ccccrrrrcccc"):
            state, _ = self._sim(pattern)
            seq = [e["theme"] for e in state["log"]]
            for i, (x, y) in enumerate(zip(seq, seq[1:])):
                self.assertNotEqual(x, y, (pattern, i, seq))

    def test_cup_keeps_its_rotation_when_there_is_no_clash(self):
        cups = [t for t in render.THEMES if t != render.FINAL_THEME]
        for posts in range(len(cups)):
            state = {"log": [{"theme": "none-of-these"}], "posts": posts}
            self.assertEqual(main.cup_theme(state), cups[posts % len(cups)])
        self.assertEqual(main.cup_theme({"log": [], "posts": 2}), cups[2])

    def test_consecutive_royales_never_share_a_theme(self):
        # the 10-slot day boundary put two royales back to back with no cup between
        for pattern in ("rrrrrrrrrrrr", "crcrcrcrcrcrcrcr", "crcrcrcrcr" + "rcrcrcrcrc" * 3,
                        "crcrcrc" + "rcrcrcr" * 4, "crrcccrrrcrcccccrr"):
            _, themes = self._sim(pattern)
            for x, y in zip(themes, themes[1:]):
                self.assertNotEqual(x, y, (pattern, themes))

    def test_royale_never_copies_the_post_just_before(self):
        state, _ = self._sim("crcrcc")
        self.assertNotEqual(main.royale_theme(state), state["log"][-1]["theme"])

    def test_theme_comes_from_the_log_not_the_cup_post_count(self):
        a = main.royale_theme({"log": [{"theme": "x"}] * 3, "posts": 0})
        b = main.royale_theme({"log": [{"theme": "x"}] * 4, "posts": 0})
        self.assertNotEqual(a, b)


class OverlapTests(unittest.TestCase):
    def test_long_name_never_reaches_the_score(self):
        """'SOUTH KOREA4': a 300 px name ran into the score digits. The widest
        score ('16', popping to 90 px) must clear the widest allowed name."""
        f = ImageFont.truetype(render.FONT_BLACK, 90)
        l, _, r, _ = f.getbbox("16")
        score_left = render.W / 2 - render.SCORE_DX - (r - l) / 2
        name_right = render.NAME_X + render.NAME_MAX_W
        self.assertGreaterEqual(score_left - name_right, 20)
        f40 = ImageFont.truetype(render.FONT_BLACK, 40)
        self.assertGreater(f40.getlength("SOUTH KOREA"), render.NAME_MAX_W)  # it does shrink


class HookTemplateTests(unittest.TestCase):
    def test_template_names_both_countries(self):
        self.assertEqual(main.match_hook("{a} vs {b}: who escapes first?", "gb-sct", "ch"),
                         "Scotland vs Switzerland: who escapes first?")

    def test_plain_hook_and_royale_home(self):
        self.assertEqual(main.match_hook("Which country escapes first?", "gb-sct", "ch"),
                         "Which country escapes first?")
        self.assertEqual(main.match_hook("Can {home} escape first?", home="gb-wls"),
                         "Can Wales escape first?")
        self.assertEqual(main.match_hook("{a} vs {b}", None, None), "{a} vs {b}")   # never raises

    def test_lever_is_off(self):
        self.assertFalse(main.HOOK_TEMPLATE_LEVER)
        main.apply_loop({"hook_text": "{a} vs {b}: who escapes first?"})
        try:
            self.assertEqual(main.hook_for_match("gb-sct", "ch"), "{a} vs {b}: who escapes first?")
        finally:
            main.apply_loop({})


def _analytics(posts):
    """posts: [(due ISO, arm, views)] with 24h/96h snapshots so views@72h exists."""
    out = {}
    for i, (due, arm, views) in enumerate(posts):
        t0 = datetime.fromisoformat(due)
        snaps = [{"at": (t0 + timedelta(hours=h)).isoformat(), "views": views * f}
                 for h, f in ((24, 0.8), (60, 1.0), (96, 1.0))]
        out[f"p{i}"] = {"variant": arm, "due": due, "length": 20, "snapshots": snaps}
    return {"posts": out}


class A1SplitTests(unittest.TestCase):
    def setUp(self):
        posts = []
        for i in range(8):
            day = f"2026-10-{14 + i // 2:02d}T12:00:00+00:00"
            posts.append((day, ("cup", "royale")[i % 2], 700 if i % 2 == 0 else 300))
        for i in range(10):
            day = f"2026-10-{21 + i // 5:02d}T{8 + i:02d}:00:00+00:00"
            posts.append((day, ("cup", "royale")[i % 2], 500 if i % 2 == 0 else 200))
        self.data = _analytics(posts)

    def test_verdict_unchanged_split_is_info_only(self):
        out = analytics.compare(self.data)
        c = [analytics.at_age(r)["views"] for r in self.data["posts"].values() if r["variant"] == "cup"]
        t = [analytics.at_age(r)["views"] for r in self.data["posts"].values() if r["variant"] == "royale"]
        verdict = growth_stats.format_result(
            "royale vs cup", "views@72h",
            growth_stats.judge(c, t, analytics.AB_MIN_N, analytics.AB_MAX_N, guardrails={
                "avg % watched": ([None] * len(c), [None] * len(t), True),
                "likes/1k": ([None] * len(c), [None] * len(t), True)})).replace("**", "")
        self.assertIn(verdict.split("\n")[0], out)
        self.assertIn("before 2026-10-21", out)
        self.assertIn("from 2026-10-21", out)
        before = next(x for x in out.split("\n") if "before 2026-10-21" in x)
        self.assertIn("cup n=4", before)
        self.assertIn("royale n=4", before)

    def test_kill_switch_line(self):
        out = analytics.compare(self.data)
        line = next(x for x in out.split("\n") if x.startswith("kill switch"))
        self.assertIn("TRIP", line)          # median of 500/200 posts since 10-21 = 350 < 375
        self.assertIn("posting.json", line)


if __name__ == "__main__":
    unittest.main()
