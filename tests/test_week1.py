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
