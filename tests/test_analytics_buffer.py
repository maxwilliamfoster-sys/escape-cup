"""
analytics.collect() reads Buffer through growth-lab buffer_client (S7, MASTER_PLAN #4 /
plan-escape #1): one paginated sent-posts query instead of one call per published post,
the quota left logged every run, and a 429 never mistaken for "no posts". Offline.

    py -m unittest tests.test_analytics_buffer -v
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import analytics  # noqa: E402
import buffer_client  # noqa: E402

LIMIT_HEADERS = [
    ("RateLimit", '"100-in-15min";r=97;t=800, "250-in-24h";r=240;t=50000, "3000-in-30d";r=2900;t=999'),
    ("RateLimit-Policy", '"100-in-15min";q=100;w=900, "250-in-24h";q=250;w=86400, '
                         '"3000-in-30d";q=3000;w=2592000'),
]


def node(pid, views):
    return {"node": {"id": pid, "status": "sent", "dueAt": None, "sentAt": "2026-10-22T10:00:00Z",
                     "text": f"caption {pid}\n#tags", "externalLink": f"https://www.tiktok.com/@5lmvill/video/{pid}",
                     "metricsUpdatedAt": "2026-10-23T09:00:00Z",
                     "metrics": [{"type": "views", "value": views}, {"type": "reactions", "value": 5}]}}


class FakeBuffer:
    """Transport for buffer_client.BufferClient: 1 org, 2 channels, sent posts in one page."""

    def __init__(self, posts, status=200, headers=LIMIT_HEADERS):
        self.posts, self.status, self.headers, self.queries = posts, status, headers, []

    def __call__(self, url, body, headers, timeout):
        q = body["query"]
        self.queries.append(q)
        if self.status == 429:
            return 429, self.headers + [("Retry-After", "600")], json.dumps(
                {"errors": [{"message": "rate", "extensions": {"code": "RATE_LIMIT_EXCEEDED", "window": "24h"}}]})
        if "organizations" in q:
            data = {"account": {"organizations": [{"id": "org1", "name": "Max"}]}}
        elif "channels(" in q:
            data = {"channels": [
                {"id": "chBCF", "name": "buriedcasefiles", "displayName": "BCF", "externalLink": "",
                 "service": "tiktok", "isQueuePaused": False},
                {"id": "chEC", "name": "5lmvill", "displayName": "Escape", "externalLink": "",
                 "service": "tiktok", "isQueuePaused": False}]}
        else:
            assert body["variables"]["c"] == ["chEC"], body["variables"]
            data = {"posts": {"edges": self.posts, "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        return 200, self.headers, json.dumps({"data": data})


STATE = {"log": [
    {"status": "published", "buffer_id": "p1", "a": "fr", "b": "br", "edition": 1, "r": 1,
     "due": "2026-10-22T10:00:00+00:00", "length": 20},
    {"status": "published", "buffer_id": "p2", "series": "royale", "heat": 3, "edition": 1,
     "due": "2026-10-22T12:00:00+00:00", "length": 25},
    {"status": "published", "buffer_id": "gone", "a": "jp", "b": "kr", "edition": 1, "r": 1},
    {"status": "scheduled", "buffer_id": "p9", "a": "us", "b": "mx", "edition": 1, "r": 1},
]}


class TestCollect(unittest.TestCase):
    def run_collect(self, fake, data=None):
        saved = {}
        client = buffer_client.BufferClient(api_key="k", transport=fake, log=lambda *_: None)
        out = io.StringIO()
        with mock.patch.object(analytics.tournament, "load", return_value=json.loads(json.dumps(STATE))), \
                mock.patch.object(analytics, "load", return_value=data or {"posts": {}}), \
                mock.patch.object(analytics, "save", side_effect=lambda d: saved.update(d)), \
                mock.patch.object(analytics, "snap_account"), \
                mock.patch.object(analytics, "_quota_alert") as alert, \
                mock.patch.dict(os.environ, {"BUFFER_CHANNEL_ID": ""}), redirect_stdout(out):
            res = analytics.collect(client=client)
        return res, saved, out.getvalue(), fake, alert

    def test_one_posts_query_whatever_the_number_of_posts(self):
        fake = FakeBuffer([node("p1", 100), node("p2", 300), node("other", 7)])
        res, saved, log, fake, _ = self.run_collect(fake)
        self.assertEqual(sum("posts(" in q for q in fake.queries), 1)
        self.assertLessEqual(len(fake.queries), 3)                  # orgs + channels + 1 page
        self.assertNotIn("post(input", "".join(fake.queries))      # no per-post query left
        self.assertEqual(set(saved["posts"]), {"p1", "p2"})        # matched by buffer_id
        self.assertEqual(res["posts"]["p1"]["snapshots"][-1]["views"], 100)
        self.assertEqual(res["posts"]["p1"]["snapshots"][-1]["likes"], 5)   # reactions -> likes
        self.assertEqual(res["posts"]["p2"]["match"], "royale 3")
        self.assertEqual(res["posts"]["p1"]["caption"], "caption p1")

    def test_logs_calls_and_remaining_quota(self):
        _, _, log, fake, alert = self.run_collect(FakeBuffer([node("p1", 1)]))
        self.assertIn(f"buffer calls: {len(fake.queries)}, remaining 24h/30d: 240/2900", log)
        alert.assert_not_called()

    def test_low_quota_alerts_bot_hq(self):
        low = [("RateLimit", '"250-in-24h";r=20;t=50000, "3000-in-30d";r=2900;t=999'),
               ("RateLimit-Policy", '"250-in-24h";q=250;w=86400, "3000-in-30d";q=3000;w=2592000')]
        _, _, log, _, alert = self.run_collect(FakeBuffer([node("p1", 1)], headers=low))
        alert.assert_called_once()
        self.assertIn("24h", alert.call_args.args[0])

    def test_a_429_raises_and_writes_nothing(self):
        saved = {}
        client = buffer_client.BufferClient(api_key="k", transport=FakeBuffer([], status=429),
                                            log=lambda *_: None)
        with mock.patch.object(analytics.tournament, "load", return_value=STATE), \
                mock.patch.object(analytics, "load", return_value={"posts": {"old": {}}}), \
                mock.patch.object(analytics, "save", side_effect=lambda d: saved.update(d)), \
                mock.patch.object(analytics, "snap_account"), redirect_stdout(io.StringIO()):
            with self.assertRaises(buffer_client.BufferRateLimited):
                analytics.collect(client=client)
        self.assertEqual(saved, {})

    def test_same_posts_as_the_old_per_post_loop(self):
        # the old loop queried every published log entry with a buffer_id and kept the ones
        # Buffer returned; the batched query must update exactly that set
        fake = FakeBuffer([node("p2", 1), node("p1", 2), node("p9", 3)])
        _, saved, _, _, _ = self.run_collect(fake)
        old = {e["buffer_id"] for e in STATE["log"] if e["status"] == "published"} & {"p1", "p2", "p9"}
        self.assertEqual(set(saved["posts"]), old)

    def test_snapshot_only_added_when_metrics_change(self):
        fake = FakeBuffer([node("p1", 100)])
        res, _, _, _, _ = self.run_collect(fake)
        again, _, _, _, _ = self.run_collect(FakeBuffer([node("p1", 100)]), data=res)
        self.assertEqual(len(again["posts"]["p1"]["snapshots"]), 1)


if __name__ == "__main__":
    unittest.main()
