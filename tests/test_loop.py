"""
Experiment-loop wiring for Escape Cup (growth-lab, 2026-10-16), on fake data.
Offline: no Buffer, no Telegram, no render.

    py -m unittest discover -s tests -v
"""
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import director  # noqa: E402
import experiment_loop as el  # noqa: E402
import loop_nightly as ln  # noqa: E402
import main  # noqa: E402
import render  # noqa: E402

FMT = "escape-cup-format-1"


def fake_analytics(n, watch_cup, watch_royale, start="2026-10-13", seed=0):
    """analytics.json with n cup + n royale posts, each with 24h, 60h and 96h snapshots
    (views@72h needs points under 48h apart)."""
    rng = random.Random(seed)
    posts = {}
    t0 = datetime.fromisoformat(start + "T15:30:00+00:00")
    for i in range(2 * n):
        arm = ("cup", "royale")[i % 2]
        due = t0 + timedelta(hours=12 * i)
        L = 20.0
        pct = (watch_cup if arm == "cup" else watch_royale) + rng.uniform(-2, 2)
        # views follow watch %, so the arm that holds better also wins on views@72h
        # (the format test's primary metric since the 2026-10-19 amendment)
        snaps = [{"at": (due + timedelta(hours=h)).isoformat(), "views": int(10 * h * pct / 24),
                  "likes": 5, "averageTimeWatched": pct / 100 * L} for h in (24, 60, 96)]
        posts[f"p{i}"] = {"variant": arm, "due": due.isoformat(), "length": L, "snapshots": snaps}
    return {"posts": posts}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for f in ("experiments.json", "levers.json", "loop_config.json"):
            shutil.copy(os.path.join(ROOT, f), self.tmp)
        mock.patch.object(ln, "HERE", self.tmp).start()

    def tearDown(self):
        mock.patch.stopall()
        shutil.rmtree(self.tmp, ignore_errors=True)
        main.apply_loop({})

    def read(self, name):
        with open(os.path.join(self.tmp, name), encoding="utf-8") as fh:
            return json.load(fh)

    def on(self, y, m, d):
        dt = mock.patch.object(ln, "datetime").start()
        dt.now.return_value = datetime(y, m, d, 12, tzinfo=timezone.utc)
        dt.fromisoformat = datetime.fromisoformat


class TestNightly(Sandbox):
    def test_api_failure_changes_nothing(self):
        self.on(2026, 11, 1)
        with mock.patch.object(ln.analytics, "load", side_effect=OSError("disk")):
            self.assertEqual(ln.main([]), 0)
        self.assertEqual(self.read("experiments.json")["experiments"][0]["status"], "running")
        self.assertIn("no decisions", " ".join(self.read("loop_summary.json")["actions"]))

    def test_empty_analytics_is_no_data(self):
        self.on(2026, 11, 1)
        with mock.patch.object(ln.analytics, "load", return_value={"posts": {}}):
            ln.main([])
        self.assertEqual(len(self.read("experiments.json")["experiments"]), 1)

    def test_royale_winner_promoted_and_next_test_started(self):
        self.on(2026, 11, 1)
        with mock.patch.object(ln.analytics, "load", return_value=fake_analytics(12, 25, 40)):
            ln.main([])
        exps = self.read("experiments.json")["experiments"]
        self.assertEqual(exps[0]["status"], "promoted")
        self.assertEqual(self.read("loop_config.json")["values"]["format"], "royale")
        self.assertEqual([e["lever"] for e in exps if e["status"] == "running"], ["hook_text"])

    def test_cup_kept_when_royale_loses(self):
        self.on(2026, 11, 1)
        with mock.patch.object(ln.analytics, "load", return_value=fake_analytics(12, 40, 25)):
            ln.main([])
        self.assertEqual(self.read("experiments.json")["experiments"][0]["status"], "reverted")
        self.assertEqual(self.read("loop_config.json")["values"]["format"], "cup")

    def test_too_few_posts_keeps_running(self):
        self.on(2026, 11, 1)
        with mock.patch.object(ln.analytics, "load", return_value=fake_analytics(6, 25, 40)):
            ln.main([])
        exps = self.read("experiments.json")["experiments"]
        self.assertEqual([e["status"] for e in exps], ["running"])

    def test_dry_run_writes_nothing(self):
        self.on(2026, 11, 1)
        with mock.patch.object(ln.analytics, "load", return_value=fake_analytics(12, 25, 40)):
            ln.main(["--dry-run"])
        self.assertEqual(self.read("experiments.json")["experiments"][0]["status"], "running")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "loop_summary.json")))

    def test_collect_on_real_file(self):
        posts, summary = ln.collect(el.load_repo(ROOT)[0])
        self.assertTrue(posts)
        self.assertIn("views", summary)


class TestRenderSide(unittest.TestCase):
    def tearDown(self):
        main.apply_loop({})

    def test_arms_follow_cup_royale_slot_alternation(self):
        state = {"log": [{"due": "2026-10-02T18:30:00+00:00"}]}
        led = {"experiments": [{"id": FMT, "lever": "format", "control": "cup",
                                "variant": "royale", "status": "running"}]}
        with mock.patch.object(el, "load_repo", return_value=(led, {}, {"values": {}})):
            for day in range(20, 24):
                d = datetime(2026, 10, day).date()
                for h, mi in main.slots_for_day(d, main.first_post_day(state, d)):
                    slot = datetime(2026, 10, day, h, mi, tzinfo=main.LONDON).astimezone(timezone.utc)
                    vals, arms = main.loop_for(slot, state)
                    self.assertEqual(main.arm_for(slot, state, vals), main.arm_for(slot, state))
                    self.assertEqual(arms[FMT], "control" if vals["format"] == "cup" else "variant")

    def test_promoted_format_decides_every_slot(self):
        slot = datetime(2026, 11, 2, 16, 30, tzinfo=main.LONDON).astimezone(timezone.utc)
        for fmt in ("cup", "royale"):
            self.assertEqual(main.arm_for(slot, {"log": []}, {"format": fmt}), fmt)

    def test_apply_loop_sets_and_resets_knobs(self):
        main.apply_loop({"pacing": "fast", "length": "short", "hook_text": "Pick a country before it starts!"})
        self.assertEqual(director.MAX_FIRST_BREAK, 1.2)
        self.assertEqual(director.MAX_SECONDS, 20.0)
        self.assertEqual(director.ROYALE_RULES["max_s"], 26.0)
        self.assertEqual(render.HOOK_TEXT, "Pick a country before it starts!")
        main.apply_loop({})                                   # next slot is control
        self.assertEqual(director.MAX_FIRST_BREAK, 2.0)
        self.assertEqual(director.MAX_SECONDS, 28.0)
        self.assertEqual(director.ROYALE_RULES, main.LOOP_DEFAULTS["royale"])
        self.assertEqual(render.HOOK_TEXT, "Which country escapes first?")

    def test_broken_loop_files_render_as_before(self):
        with mock.patch.object(el, "load_repo", side_effect=ValueError("bad json")):
            self.assertEqual(main.loop_for(datetime.now(timezone.utc), {"log": []}), ({}, {}))

    def test_fast_pacing_still_finds_a_match(self):
        main.apply_loop({"pacing": "fast"})
        seed, d = director.pick(4242)
        self.assertLessEqual(d["first_break"], 1.2)
        self.assertLessEqual(d["longest_wait"], 4.5)

    def test_every_lever_value_is_one_apply_loop_knows(self):
        lev = el.load_json(os.path.join(ROOT, "levers.json"), {})
        known = {"format": {"cup", "royale"}, "pacing": {"normal", "fast"},
                 "length": {"normal", "short"}}
        for l in lev["levers"]:
            vals = [l["control"]] + l["variants"]
            if l["lever"] == "hook_text":
                self.assertTrue(all(isinstance(v, str) and 0 < len(v) <= 40 for v in vals))
            else:
                self.assertTrue(set(vals) <= known[l["lever"]], l["lever"])


class TestFormatAmendment(unittest.TestCase):
    """2026-10-19: the format test is judged on views@72h (EXPERIMENTS.md, A1)."""

    def test_format_lever_judged_on_views(self):
        with open(os.path.join(ROOT, "levers.json"), encoding="utf-8") as fh:
            levers = json.load(fh)
        fmt = [l for l in levers["levers"] if l["lever"] == "format"][0]
        self.assertEqual(fmt["primary"], "views72")

    def test_backfilled_lengths_cover_ab_start(self):
        import analytics
        with open(os.path.join(ROOT, "tournament_state.json"), encoding="utf-8") as fh:
            log = json.load(fh)["log"]
        missing = [e.get("due") for e in log if (e.get("due") or "") >= analytics.AB_FROM
                   and not e.get("length")
                   and not analytics.BACKFILL_LEN.get(f"{e.get('a')}-{e.get('b')}")]
        self.assertEqual(missing, [])

    def test_compare_report_judges_on_views(self):
        import analytics
        out = analytics.compare({"posts": {}})
        self.assertIn("views@72h", out.splitlines()[2])
        self.assertNotIn("avg % watched @72h", out.splitlines()[2])


if __name__ == "__main__":
    unittest.main()
