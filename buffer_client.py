# vendored from growth-lab/buffer_client.py @ 460d0a43dd36de57fbf3cb904bd92ca88a91001d
# Do not edit here: change the growth-lab master and run sync_vendored.py.
"""buffer_client: one lean Buffer GraphQL client for every Buffer bot (growth-lab S7, master copy).

Edit it HERE, then roll it out with sync_vendored.py. Stdlib only.

    import buffer_client as bc
    c = bc.BufferClient(cache_path="buffer_ids.json")      # key from $BUFFER_API_KEY
    org, chan = c.find_channel(service="tiktok", exclude_handles=("buriedcasefiles",))
    posts = c.sent_posts(org, chan)          # ONE paginated query, metrics included
    print(c.remaining())                     # {"15m": {...}, "24h": {...}, "30d": {...}}
    if c.quota_low(): ...                    # windows under 15% left: warn in Bot HQ

Rate limits (read from the Buffer developer docs on 2026-10-23,
https://developers.buffer.com/guides/api-limits.html). Limits are per client
(per API key), in rolling windows:

    plan        15 min   24 h   30 days
    Free         100      250    3,000     (3,000 / 30 = 100 a day on average)
    Essentials   100      250    7,500
    Team         100      500   15,000

So both earlier figures were half right: post.yml's "250/day" is the hard 24 h
window, and the audit's "about 100/day" is the 30-day budget averaged out. The
30-day window is the one a daily cadence of 80-90 calls actually runs into.
Escape, BCF and space all use secrets.BUFFER_API_KEY: if that is the SAME key,
they share one set of windows.

Every reply carries one `RateLimit` and one `RateLimit-Policy` header per window:

    RateLimit: "100-in-15min";r=98;t=897            r = remaining, t = seconds to reset
    RateLimit-Policy: "100-in-15min";q=100;w=900;pk=:...:   q = quota, w = window (s)

Policies are matched by `w` (900 / 86400 / 2592000), since the names change
with the plan. A 429 has `Retry-After` (s), the tripped policy with r=0, and
errors[0].extensions.code == "RATE_LIMIT_EXCEEDED" with extensions.window
("15m"...). A 429 does not use up quota. It is raised as BufferRateLimited and is
NEVER turned into an empty result: "no posts" and "Buffer refused" must not look
alike (a 429 read as "missing" would make reconcile re-post or drop videos).

Pagination is cursor based: posts(first:, after:) + pageInfo{hasNextPage endCursor}
(https://developers.buffer.com/guides/pagination.html).

The org and channel ids are cached in a small JSON file (with the last quota
snapshot), so a normal run spends no calls on lookups.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BUFFER_API = "https://api.buffer.com"
DOCS_URL = "https://developers.buffer.com/guides/api-limits.html"
USER_AGENT = "growth-lab-buffer-client/1.0"
TIMEOUT = 60

WINDOWS = {900: "15m", 86400: "24h", 2592000: "30d"}
_NAME_UNITS = {"min": 60, "mins": 60, "minute": 60, "minutes": 60, "h": 3600, "hr": 3600,
               "hour": 3600, "hours": 3600, "day": 86400, "days": 86400}
LOW_FRACTION = 0.15

# Field names checked against Buffer's public introspection schema on 2026-10-23.
POST_FIELDS = "id status dueAt sentAt text externalLink metricsUpdatedAt metrics { type value }"
SENT_POSTS_QUERY = """query($o: OrganizationId!, $c: [ChannelId!], $n: Int, $a: String) {
  posts(first: $n, after: $a, input: {organizationId: $o, filter: {status: [sent], channelIds: $c}}) {
    edges { node { %s } }
    pageInfo { hasNextPage endCursor } } }""" % POST_FIELDS
ORGS_QUERY = "query { account { organizations { id name } } }"
CHANNELS_QUERY = ("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
                  "{ id name displayName externalLink service isQueuePaused } }")


class BufferError(RuntimeError):
    pass


class BufferRateLimited(BufferError):
    """HTTP 429 / RATE_LIMIT_EXCEEDED. Not a missing post. Stop and retry after
    `retry_after` seconds; `window` is "15m", "24h" or "30d" when Buffer says."""

    def __init__(self, msg, retry_after=None, window=None, limits=None):
        super().__init__(msg)
        self.retry_after, self.window, self.limits = retry_after, window, limits or {}


# ------------------------------------------------------------------ headers --

_RL = re.compile(r'"([^"]+)"\s*;\s*r=(\d+)\s*;\s*t=(\d+)')
_POL = re.compile(r'"([^"]+)"\s*;\s*q=(\d+)\s*;\s*w=(\d+)')
_NAME = re.compile(r"(\d+)-in-(\d*)\s*([a-z]+)", re.I)


def _window_from_name(name):
    m = _NAME.search(name or "")
    if not m:
        return None, None
    n = int(m.group(2) or 1)
    unit = _NAME_UNITS.get(m.group(3).lower())
    return int(m.group(1)), (n * unit if unit else None)


def parse_ratelimit(headers):
    """headers: [(name, value), ...] (duplicates kept) or a dict. Returns
    {"15m": {"limit", "remaining", "reset_s", "fraction", "name"}, ...}."""
    items = headers.items() if isinstance(headers, dict) else headers
    rl, pol = [], []
    for k, v in items:
        k = k.lower()
        if k == "ratelimit":
            rl += _RL.findall(v or "")
        elif k == "ratelimit-policy":
            pol += _POL.findall(v or "")
    policy = {name: (int(q), int(w)) for name, q, w in pol}
    out = {}
    for name, r, t in rl:
        q, w = policy.get(name, (None, None))
        if q is None or w is None:
            q2, w2 = _window_from_name(name)
            q, w = q if q is not None else q2, w if w is not None else w2
        key = WINDOWS.get(w, f"{w}s" if w else name)
        rem = int(r)
        out[key] = {"name": name, "limit": q, "remaining": rem, "reset_s": int(t),
                    "fraction": round(rem / q, 4) if q else None}
    return out


def format_limits(limits):
    order = sorted(limits, key=lambda k: list(WINDOWS.values()).index(k)
                   if k in WINDOWS.values() else 9)
    return ", ".join(f"{k} {limits[k]['remaining']}/{limits[k]['limit']}" for k in order) or "none"


# ---------------------------------------------------------------- transport --

def _http_post(url, body, headers, timeout):
    """Returns (status, [(header, value), ...], text). Raises OSError on network failure."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, list(r.headers.items()), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        hdrs = list(e.headers.items()) if e.headers else []
        e.close()
        return e.code, hdrs, text


# ------------------------------------------------------------------- client --

class BufferClient:
    def __init__(self, api_key=None, cache_path=None, transport=None, log=print,
                 low_fraction=LOW_FRACTION, timeout=TIMEOUT):
        self.api_key = api_key if api_key is not None else os.environ.get("BUFFER_API_KEY", "")
        self.cache_path = cache_path
        self.transport = transport or _http_post
        self.log = log or (lambda *_: None)
        self.low_fraction = low_fraction
        self.timeout = timeout
        self.limits = {}
        self.calls = 0

    # -- core --
    def gql(self, query, variables=None):
        if not self.api_key:
            raise BufferError("BUFFER_API_KEY not set")
        status, headers, text = self.transport(
            BUFFER_API, {"query": query, "variables": variables or {}},
            {"Authorization": f"Bearer {self.api_key}"}, self.timeout)
        self.calls += 1
        lim = parse_ratelimit(headers)
        if lim:
            self.limits.update(lim)
            self._log_limits()
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = {}
        errors = body.get("errors") or [] if isinstance(body, dict) else []
        rate_err = next((e for e in errors
                         if (e.get("extensions") or {}).get("code") == "RATE_LIMIT_EXCEEDED"), None)
        if status == 429 or rate_err is not None:
            ra = next((v for k, v in headers if k.lower() == "retry-after"), None)
            try:
                ra = float(ra) if ra is not None else None
            except ValueError:
                ra = None
            window = ((rate_err or {}).get("extensions") or {}).get("window")
            if window is None:
                window = next((k for k, v in lim.items() if v["remaining"] == 0), None)
            self._save_limits()
            raise BufferRateLimited(
                f"Buffer rate limit hit (window {window or '?'}, retry after {ra}s); "
                f"quota {format_limits(self.limits)}", retry_after=ra, window=window,
                limits=dict(self.limits))
        if status != 200:
            raise BufferError(f"Buffer HTTP {status}: {text[:300]}")
        if errors:
            raise BufferError(f"Buffer error: {errors[0].get('message')}")
        if not isinstance(body, dict) or "data" not in body:
            raise BufferError(f"Buffer reply has no data: {text[:200]}")
        return body["data"]

    def _log_limits(self):
        low = self.quota_low()
        msg = f"[buffer] quota left: {format_limits(self.limits)}"
        if low:
            msg += f"  WARNING below {int(self.low_fraction * 100)}% in {', '.join(low)}"
        self.log(msg)

    # -- quota --
    def remaining(self):
        """Last seen quota per window: {"15m": {"limit","remaining","reset_s","fraction"}...}."""
        return {k: dict(v) for k, v in self.limits.items()}

    def quota_low(self, fraction=None):
        f = self.low_fraction if fraction is None else fraction
        return [k for k, v in self.limits.items()
                if v.get("fraction") is not None and v["fraction"] < f]

    # -- id cache --
    def _load_cache(self):
        if not self.cache_path:
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_cache(self, d):
        if not self.cache_path:
            return
        tmp = self.cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.cache_path)
        except OSError as e:
            self.log(f"[buffer] id cache not saved ({e})")

    def _save_limits(self):
        if self.cache_path and self.limits:
            d = self._load_cache()
            d["limits"] = self.limits
            d["limits_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._write_cache(d)

    def forget_ids(self):
        d = self._load_cache()
        for k in ("org_id", "channel_id", "channel_name", "service"):
            d.pop(k, None)
        self._write_cache(d)

    def find_channel(self, service="tiktok", channel_id=None, exclude_handles=()):
        """(org_id, channel_id). Uses the JSON cache when it matches; otherwise
        looks the channel up (1 + one call per org) and caches it. Refuses to
        guess when more than one channel fits (set channel_id)."""
        d = self._load_cache()
        if d.get("org_id") and d.get("channel_id") and (
                (channel_id and d["channel_id"] == channel_id)
                or (not channel_id and d.get("service") == service)):
            return d["org_id"], d["channel_id"]
        orgs = self.gql(ORGS_QUERY)["account"]["organizations"]
        hits = []
        excl = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in exclude_handles or ()]
        for org in orgs:
            for ch in self.gql(CHANNELS_QUERY, {"o": org["id"]})["channels"]:
                ident = re.sub(r"[^a-z0-9]", "", "{}{}{}".format(
                    ch.get("name") or "", ch.get("displayName") or "",
                    ch.get("externalLink") or "").lower())
                if channel_id:
                    ok = ch["id"] == channel_id
                else:
                    ok = ch.get("service") == service and not any(h and h in ident for h in excl)
                if ok:
                    hits.append((org["id"], ch))
        if len(hits) != 1:
            raise BufferError(f"expected 1 {service} channel in Buffer, found {len(hits)}: "
                              f"{[c.get('name') for _, c in hits]} - pass channel_id")
        org_id, ch = hits[0]
        d.update(org_id=org_id, channel_id=ch["id"], channel_name=ch.get("name"),
                 service=ch.get("service"),
                 cached_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        if self.limits:
            d["limits"] = self.limits
        self._write_cache(d)
        return org_id, ch["id"]

    # -- posts --
    def sent_posts(self, org_id, channel_id, first=100, max_pages=5):
        """Every sent post (newest first) with metrics, as plain dicts with a
        `metrics` dict {type: value}. One query per page of `first` (100), at
        most `max_pages` pages. Raises BufferRateLimited part-way rather than
        return a partial list as if it were complete."""
        out, after = [], None
        for _ in range(max_pages):
            data = self.gql(SENT_POSTS_QUERY,
                            {"o": org_id, "c": [channel_id], "n": int(first), "a": after})
            posts = data["posts"]
            for e in posts.get("edges") or []:
                node = dict(e["node"])
                node["metrics"] = {m["type"]: m["value"] for m in (node.get("metrics") or [])}
                out.append(node)
            info = posts.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            after = info["endCursor"]
        else:
            self.log(f"[buffer] stopped after {max_pages} pages ({len(out)} posts)")
        self._save_limits()
        return out


def tiktok_video_id(post):
    m = re.search(r"/video/(\d+)", post.get("externalLink") or "")
    return m.group(1) if m else None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Read-only Buffer check: sent posts + quota")
    ap.add_argument("--cache", default="buffer_ids.json")
    ap.add_argument("--service", default="tiktok")
    ap.add_argument("--channel-id")
    ap.add_argument("--exclude", default="", help="comma-separated handles to skip")
    a = ap.parse_args(argv)
    c = BufferClient(cache_path=a.cache)
    try:
        org, chan = c.find_channel(a.service, a.channel_id,
                                   [h for h in a.exclude.split(",") if h])
        posts = c.sent_posts(org, chan)
    except BufferRateLimited as e:
        print(f"[buffer] RATE LIMITED: {e}")
        return 4
    print(f"[buffer] {len(posts)} sent posts, {c.calls} calls; quota {format_limits(c.limits)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
