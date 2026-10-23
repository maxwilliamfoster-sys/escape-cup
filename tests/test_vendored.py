"""Vendored growth-lab modules must match vendored.lock (written by growth-lab/sync_vendored.py).

A red test here means someone edited a vendored file (upload_gate.py, experiment_loop.py, ...)
in this repo by hand. Make the change in the growth-lab master and run
`py sync_vendored.py --bot <this bot>` instead (or `--force-adopt` for a deliberate local patch).
Template: growth-lab/templates/test_vendored.py - copy unchanged into each bot's tests/.
"""
import hashlib
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, "vendored.lock")


def norm_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


class Vendored(unittest.TestCase):
    def test_vendored_files_match_the_lock(self):
        if not os.path.exists(LOCK):
            self.skipTest("no vendored.lock yet (run growth-lab/sync_vendored.py)")
        with open(LOCK, encoding="utf-8") as fh:
            files = json.load(fh).get("files") or {}
        self.assertTrue(files, "vendored.lock lists no files")
        for name, entry in sorted(files.items()):
            with self.subTest(file=name):
                path = os.path.join(ROOT, name)
                self.assertTrue(os.path.exists(path), f"{name} is in vendored.lock but missing")
                self.assertEqual(norm_sha(path), entry["sha256"],
                                 f"{name} differs from vendored.lock: edit the growth-lab master and run "
                                 f"sync_vendored.py, don't patch the vendored copy here")


if __name__ == "__main__":
    unittest.main()
