"""publish.host_video: never force-push an emptied media branch; retrigger a stuck Pages
deploy once; tokens never reach error messages (audit 2026-10-19, E3/E11)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import publish  # noqa: E402
import upload_gate  # noqa: E402


class FakeGit:
    """Records every git command; ls-remote/fetch/checkout behave as configured."""

    def __init__(self, branch=True, fetch_ok=True, files=()):
        self.branch, self.fetch_ok, self.files, self.cmds = branch, fetch_ok, files, []

    def __call__(self, cmd, cwd=None, **kw):
        self.cmds.append(cmd)
        out, code = "", 0
        if "ls-remote" in cmd:
            out = "abc123\trefs/heads/media\n" if self.branch else ""
        elif "fetch" in cmd:
            code = 0 if self.fetch_ok else 1
        elif "FETCH_HEAD" in cmd:
            os.makedirs(os.path.join(cwd, "v"), exist_ok=True)
            for f in self.files:
                open(os.path.join(cwd, "v", f), "wb").close()
        return mock.Mock(returncode=code, stdout=out, stderr="")

    def pushed(self):
        return [c for c in self.cmds if "push" in c]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestHosting(unittest.TestCase):
    def setUp(self):
        fd, self.video = tempfile.mkstemp(suffix=".mp4")
        os.write(fd, b"video"); os.close(fd)
        mock.patch.object(upload_gate, "require", lambda *a: "sha").start()
        mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghs_SECRET", "GITHUB_REPOSITORY": "o/r"}).start()
        self.clock = FakeClock()
        mock.patch.object(publish, "time", self.clock).start()
        self.head = mock.patch.object(publish.requests, "head", return_value=mock.Mock(
            status_code=200, headers={"content-length": "5"})).start()

    def tearDown(self):
        mock.patch.stopall()
        os.remove(self.video)

    def _host(self, git, protect=()):
        with mock.patch.object(publish.subprocess, "run", git):
            return publish.host_video(self.video, "new.mp4", protect=protect)

    def test_fetch_failure_aborts_when_branch_exists(self):
        git = FakeGit(branch=True, fetch_ok=False)
        with self.assertRaises(publish.PublishError) as cm:
            self._host(git)
        self.assertIn("could not be fetched", str(cm.exception))
        self.assertEqual(git.pushed(), [])

    def test_missing_protected_file_aborts(self):
        git = FakeGit(files=["b.mp4"])
        with self.assertRaises(publish.PublishError) as cm:
            self._host(git, protect={"a.mp4"})
        self.assertIn("a.mp4", str(cm.exception))
        self.assertEqual(git.pushed(), [])

    def test_first_upload_without_branch_still_works(self):
        git = FakeGit(branch=False, fetch_ok=False)
        url = self._host(git)
        self.assertTrue(url.endswith("/v/new.mp4"))
        self.assertEqual(len(git.pushed()), 1)

    def test_stuck_pages_deploy_is_retriggered_once(self):
        def head(url, **kw):
            ok = any("--allow-empty" in c for c in git.cmds) and self.clock.t > 400
            return mock.Mock(status_code=200 if ok else 500, headers={"content-length": "5"})
        self.head.side_effect = head
        git = FakeGit(files=["a.mp4"])
        self._host(git, protect={"a.mp4"})
        self.assertEqual(sum("--allow-empty" in c for c in git.cmds), 1)
        self.assertEqual(len(git.pushed()), 2)

    def test_git_errors_never_show_the_token(self):
        def run(cmd, cwd=None, **kw):
            return mock.Mock(returncode=128, stdout="",
                             stderr="fatal: https://x-access-token:ghs_SECRET@github.com/o/r.git")
        with mock.patch.object(publish.subprocess, "run", run), \
                self.assertRaises(publish.PublishError) as cm:
            publish._git(["push", "-q"], ".")
        self.assertNotIn("ghs_SECRET", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
