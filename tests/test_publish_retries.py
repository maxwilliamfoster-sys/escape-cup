"""Flaky-network retries in the publisher: git pushes and Buffer's "video could not
be read" refusal are tried again instead of failing the whole run."""
import importlib
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
MODULE = "publish"
pub = importlib.import_module(MODULE)


def proc(code, err=""):
    return types.SimpleNamespace(returncode=code, stdout="ok", stderr=err)


class GitRetries(unittest.TestCase):
    def run_git(self, args, results):
        with mock.patch.object(pub.subprocess, "run", side_effect=results) as run, \
                mock.patch.object(pub.time, "sleep") as sleep:
            try:
                out = pub._git(args, ".")
            except pub.PublishError as e:
                out = e
        return out, run.call_count, sleep.call_count

    def test_push_is_tried_again_after_a_remote_hiccup(self):
        out, calls, sleeps = self.run_git(
            ["push", "-q", "-f", "remote", "media"],
            [proc(1, "remote: fatal error in commit_refs_rules"), proc(0)])
        self.assertEqual((out, calls, sleeps), ("ok", 2, 1))

    def test_push_gives_up_after_every_retry(self):
        n = len(pub.GIT_RETRY_WAITS) + 1
        out, calls, _ = self.run_git(["push", "-q", "remote", "media"], [proc(1, "boom")] * n)
        self.assertIsInstance(out, pub.PublishError)
        self.assertEqual(calls, n)

    def test_local_commands_fail_at_once(self):
        out, calls, sleeps = self.run_git(["add", "-A"], [proc(1, "bad")])
        self.assertIsInstance(out, pub.PublishError)
        self.assertEqual((calls, sleeps), (1, 0))

    def test_config_flags_before_the_verb_still_count_as_network(self):
        out, calls, _ = self.run_git(["-c", "user.name=x", "push", "-q"], [proc(1, "x"), proc(0)])
        self.assertEqual((out, calls), ("ok", 2))

    def test_token_never_reaches_the_error(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "s3cret-token"}):
            out, _, _ = self.run_git(["add", "-A"], [proc(1, "https://x:s3cret-token@github.com denied")])
        self.assertNotIn("s3cret-token", str(out))


class CreatePostRetries(unittest.TestCase):
    def create(self, replies):
        with mock.patch.object(pub, "_gql", side_effect=[{"createPost": r} for r in replies]) as gql, \
                mock.patch.object(pub.time, "sleep"):
            return pub._create_post("q", {}), gql.call_count

    def test_unreadable_video_is_asked_again(self):
        res, calls = self.create([{"message": "Invalid post: Video could not be read from its URL."},
                                  {"post": {"id": "p1"}}])
        self.assertEqual((res["post"]["id"], calls), ("p1", 2))

    def test_other_refusals_are_not_repeated(self):
        res, calls = self.create([{"message": "Caption too long"}])
        self.assertEqual((res["message"], calls), ("Caption too long", 1))

    def test_gives_up_with_the_last_refusal(self):
        n = len(pub.CREATE_RETRY_WAITS) + 1
        res, calls = self.create([{"message": "Video could not be read from its URL."}] * n)
        self.assertNotIn("post", res)
        self.assertEqual(calls, n)


if __name__ == "__main__":
    unittest.main()
