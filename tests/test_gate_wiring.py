"""
Upload-gate wiring + review fixes (2026-10-16), offline.

    py -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import main  # noqa: E402
import publish  # noqa: E402
import upload_gate  # noqa: E402


class TestPrune(unittest.TestCase):
    def test_prunes_oldest_upload_not_alphabetical(self):
        present = {f"royale00{i}.mp4" for i in range(1, 8)} | {"ec01_r2_m05.mp4"}
        order = [f"royale00{i}.mp4" for i in range(1, 5)] + ["ec01_r2_m05.mp4"] + \
                [f"royale00{i}.mp4" for i in range(5, 8)]
        delete, new = publish.prune_plan(present, order, "new.mp4", keep=6)
        self.assertEqual(delete, ["royale001.mp4", "royale002.mp4", "royale003.mp4"])
        self.assertIn("ec01_r2_m05.mp4", new)            # newer than royale001-004: kept
        self.assertEqual(new[-1], "new.mp4")

    def test_never_deletes_a_scheduled_file(self):
        present = {"a.mp4", "b.mp4", "c.mp4"}
        delete, _ = publish.prune_plan(present, ["a.mp4", "b.mp4", "c.mp4"], "d.mp4",
                                       protect={"a.mp4"}, keep=2)
        self.assertNotIn("a.mp4", delete)

    def test_unlisted_files_count_as_oldest(self):
        delete, _ = publish.prune_plan({"old.mp4", "x.mp4"}, ["x.mp4"], "y.mp4", keep=2)
        self.assertEqual(delete, ["old.mp4"])

    def test_host_video_refuses_a_file_the_gate_never_approved(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(b"not checked")
        try:
            with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}), \
                    mock.patch.object(publish, "_git") as git:
                with self.assertRaises(upload_gate.GateBlocked):
                    publish.host_video(fh.name, "x.mp4")
                git.assert_not_called()                   # nothing was pushed
        finally:
            os.remove(fh.name)


class TestRetries(unittest.TestCase):
    def test_failed_seeds_are_saved_for_the_next_run(self):
        state = {"excluded_seeds": {"k": [1]}}
        seeds = iter([2, 3, 4])

        def build(st, exclude_seeds):
            return {"seed": next(seeds), "failures": ["loudness"]}
        with mock.patch.object(main, "MAX_ATTEMPTS", 3):
            v, reasons = main._build_with_retries(state, "k", build)
        self.assertIsNone(v)
        self.assertEqual(state["excluded_seeds"]["k"], [1, 2, 3, 4])
        self.assertEqual(len(reasons), 3)

    def test_keeps_retrying_past_three_rejections(self):
        seeds = iter(range(10, 30))

        def build(st, exclude_seeds):
            s = next(seeds)
            return {"seed": s, "failures": [] if s == 17 else ["AI review: blocked"]}
        v, reasons = main._build_with_retries({}, "k", build)
        self.assertEqual(v["seed"], 17)
        self.assertEqual(len(reasons), 7)

    def test_stops_when_the_run_is_out_of_time(self):
        seeds = iter(range(100))

        def build(st, exclude_seeds):
            return {"seed": next(seeds), "failures": ["x"]}
        with mock.patch.object(main, "_RUN_START", main.time.monotonic() - main.RUN_BUDGET_S):
            v, reasons = main._build_with_retries({}, "k", build)
        self.assertIsNone(v)
        self.assertEqual(len(reasons), 2)          # one render, then "out of time"
        self.assertIn("out of time", reasons[-1])

    def test_director_error_is_a_gate_failure_not_a_crash(self):
        def build(st, exclude_seeds):
            raise RuntimeError("no acceptable seed in 400 tries")
        v, reasons = main._build_with_retries({}, "k", build)
        self.assertIsNone(v)
        self.assertIn("no usable simulation", reasons[0])

    def test_caption_only_failure_keeps_seed_and_stops(self):
        calls = []

        def build(st, exclude_seeds):
            calls.append(1)
            return {"seed": 9, "failures": ["caption: repeat"], "caption_only": True}
        state = {"excluded_seeds": {}}
        v, reasons = main._build_with_retries(state, "k", build)
        self.assertIsNone(v)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["excluded_seeds"].get("k", []), [])

    def test_same_infrastructure_failure_three_times_stops(self):
        seeds = iter(range(50, 80))
        calls = []

        def build(st, exclude_seeds):
            calls.append(1)
            return {"seed": next(seeds), "failures": ["Video check could not run: ffprobe missing"]}
        v, reasons = main._build_with_retries({}, "k", build)
        self.assertIsNone(v)
        self.assertEqual(len(calls), 3)
        self.assertIn("same failure 3 times in a row", reasons[-1])

    def test_gate_profile_caps_daily_ai_reviews(self):
        with mock.patch.object(main.upload_gate, "check", return_value={"ok": True}) as chk:
            main.gate("x.mp4", "c", {}, (1, 2))
        self.assertEqual(chk.call_args.kwargs["profile"]["ai_daily_cap"], 8)

    def test_first_passing_render_is_used(self):
        seeds = iter([5, 6])

        def build(st, exclude_seeds):
            s = next(seeds)
            return {"seed": s, "failures": [] if s == 6 else ["x"]}
        v, _ = main._build_with_retries({}, "k", build)
        self.assertEqual(v["seed"], 6)


if __name__ == "__main__":
    unittest.main()
