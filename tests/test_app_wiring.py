"""Alerts also go to Bot HQ (shadow mode: Telegram keeps sending). Offline.

    py -m unittest tests.test_app_wiring -v
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app_events  # noqa: E402
import main  # noqa: E402
import publish  # noqa: E402
import tournament  # noqa: E402
from tests.test_royale import _cup_entries, _royale_entry  # noqa: E402


class TestReconcileEmits(unittest.TestCase):
    def setUp(self):
        self.emits = []
        for p in (mock.patch.object(app_events, "emit", lambda *a, **k: self.emits.append((a, k)) or True),
                  mock.patch.object(main.notify, "send", lambda *a, **k: True)):
            p.start()
            self.addCleanup(p.stop)

    def test_orphan_is_an_action_once(self):
        state = {"log": [{"buffer_id": "known", "status": "published"}]}
        pending = [{"id": "orphan", "dueAt": "2026-10-20T19:30:00Z"}]
        with mock.patch.object(publish, "pending_posts", return_value=pending), \
                mock.patch.object(publish, "delete_post"):
            state = main.reconcile(state, "org", "chan")
            main.reconcile(state, "org", "chan")
        self.assertEqual([(a[0], a[1], a[2]) for a, k in self.emits], [("escape", "orphan_post", "action")])
        self.assertIn("2026-10-20 19:30", self.emits[0][0][4])

    def test_dropped_heat_that_rewinds_is_a_warn(self):
        state = tournament.rebuild(_cup_entries(1) + [_royale_entry(1, status="scheduled", buffer_id="gone")])
        with mock.patch.object(publish, "pending_posts", return_value=[]), \
                mock.patch.object(publish, "get_post", return_value=None):
            main.reconcile(state, "org", "chan")
        (a, k), = self.emits
        self.assertEqual(a[1:3], ("post_rewound", "warn"))
        self.assertIn("Escape Royale heat 1", a[3])


if __name__ == "__main__":
    unittest.main()
