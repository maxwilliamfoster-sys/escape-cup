"""
Get a rendered MP4 onto TikTok with the PC off.

1. host_video(): Buffer only accepts a public, direct (no-redirect) HTTPS URL
   and fetches it at publish time, possibly hours later. We force-push the
   video to this repo's `media` branch, which GitHub Pages serves as
   https://<owner>.github.io/<repo>/v/<file>.mp4. The branch is an orphan
   rewritten every time and keeps only the last KEEP videos, so repo history
   never grows.
2. schedule_tiktok(): Buffer GraphQL createPost, customScheduled at the slot.
   Buffer is an approved TikTok partner, so posts go out PUBLIC via the
   official API - no browser automation (that got BuriedCasefiles shadowbanned).
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import requests

BUFFER_API = "https://api.buffer.com"
KEEP = 12
PAGES_WAIT_S = 600          # how long to wait for Pages to serve a new video
PAGES_RETRIGGER_S = 300     # after this long, push an empty commit to redeploy, once
OTHER_PROJECT_HANDLES = ("buriedcasefiles",)   # TikToks in the same Buffer account


class PublishError(RuntimeError):
    pass


# ---------------------------------------------------------------- hosting --

def _git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        err = r.stderr.strip()
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            err = err.replace(token, "***")     # the remote URL carries the token
        raise PublishError(f"git {' '.join(args[:2])} failed: {err[:300]}")
    return r.stdout


def pages_base():
    repo = os.environ["GITHUB_REPOSITORY"]            # owner/name
    owner, name = repo.split("/")
    return f"https://{owner.lower()}.github.io/{name}"


def prune_plan(present, order, filename, protect=(), keep=None):
    """(files to delete, new upload order ending with filename). Oldest uploads go
    first; files missing from the manifest (pre-manifest uploads) count as oldest;
    protected files are never deleted."""
    keep = keep or KEEP
    order = sorted(set(present) - set(order) - {filename}) + \
        [f for f in order if f in present and f != filename]
    removable = [f for f in order if f not in set(protect)]
    delete = []
    while len(order) > keep - 1 and removable:
        f = removable.pop(0)
        order.remove(f)
        delete.append(f)
    return delete, order + [filename]


def host_video(path, filename, protect=()):
    """protect = file names still needed by scheduled posts (never pruned)."""
    # Only a file the upload gate approved in this run may be published.
    import upload_gate
    upload_gate.require(path, "tiktok")
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise PublishError("GITHUB_TOKEN / GITHUB_REPOSITORY not set (hosting only runs in CI)")
    remote = f"https://x-access-token:{token}@github.com/{repo}.git"
    tmp = tempfile.mkdtemp()
    try:
        _git(["init", "-q"], tmp)
        _git(["checkout", "-q", "--orphan", "media"], tmp)
        os.makedirs(os.path.join(tmp, "v"), exist_ok=True)
        # carry over the most recent videos (Buffer may still need them). The push
        # below is a force-push, so if the branch exists but can't be read, stop:
        # pushing anyway would delete every video a scheduled post still needs.
        exists = bool(_git(["ls-remote", "--heads", remote, "media"], tmp).strip())
        fetched = subprocess.run(["git", "fetch", "-q", "--depth", "1", remote, "media"],
                                 cwd=tmp, capture_output=True).returncode == 0
        if fetched:
            fetched = subprocess.run(["git", "checkout", "-q", "FETCH_HEAD", "--", "."],
                                     cwd=tmp, capture_output=True).returncode == 0
        if exists and not fetched:
            raise PublishError("media branch exists but could not be fetched - not "
                               "force-pushing (would delete scheduled videos)")
        # Prune by UPLOAD ORDER (v/order.txt), never by name, and never a file a
        # scheduled post still needs. Sorting by name deleted the cup videos first
        # ("ec..." < "royale...") while Buffer was still due to fetch them.
        vdir = os.path.join(tmp, "v")
        manifest = os.path.join(vdir, "order.txt")
        order = []
        if os.path.exists(manifest):
            with open(manifest, encoding="utf-8") as fh:
                order = [ln.strip() for ln in fh if ln.strip()]
        present = {f for f in os.listdir(vdir) if f.endswith(".mp4")}
        missing = sorted(set(protect) - present - {filename})
        if missing:
            raise PublishError(f"scheduled video {missing[0]} missing from media branch - not force-pushing")
        delete, order = prune_plan(present, order, filename, protect)
        for f in delete:
            os.remove(os.path.join(vdir, f))
        shutil.copy(path, os.path.join(vdir, filename))
        with open(manifest, "w", encoding="utf-8") as fh:
            fh.write("\n".join(order) + "\n")
        open(os.path.join(tmp, ".nojekyll"), "w").close()
        with open(os.path.join(tmp, "index.html"), "w") as fh:
            fh.write("<!doctype html><title>Escape Cup media</title>")
        _git(["add", "-A"], tmp)
        _git(["-c", "user.name=github-actions[bot]",
              "-c", "user.email=github-actions[bot]@users.noreply.github.com",
              "commit", "-q", "-m", f"media: {filename}"], tmp)
        _git(["push", "-q", "-f", remote, "media"], tmp)

        url = f"{pages_base()}/v/{filename}"
        size = os.path.getsize(path)
        start = time.time()
        retriggered = False
        while time.time() < start + PAGES_WAIT_S:
            try:
                r = requests.head(url, timeout=20, allow_redirects=False)
                if r.status_code == 200 and int(r.headers.get("content-length", 0)) == size:
                    print(f"[host] live: {url}")
                    return url
            except requests.RequestException:
                pass
            # A Pages deploy sometimes fails (HTTP 500) and nothing redeploys it.
            # An empty commit starts a new deploy; once is enough.
            if not retriggered and time.time() - start >= PAGES_RETRIGGER_S:
                retriggered = True
                print("[host] Pages still not serving the video - retriggering the deploy once")
                _git(["-c", "user.name=github-actions[bot]",
                      "-c", "user.email=github-actions[bot]@users.noreply.github.com",
                      "commit", "--allow-empty", "-q", "-m", "media: retrigger Pages"], tmp)
                _git(["push", "-q", remote, "media"], tmp)
            time.sleep(15)
        raise PublishError(f"Pages never served {url} (is Pages enabled on the media branch?)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------- buffer --

def _gql(query, variables=None):
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        raise PublishError("BUFFER_API_KEY not set")
    r = requests.post(BUFFER_API, json={"query": query, "variables": variables or {}},
                      headers={"Authorization": f"Bearer {key}"}, timeout=60)
    if r.status_code != 200:
        raise PublishError(f"Buffer HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get("errors"):
        raise PublishError(f"Buffer error: {body['errors'][0].get('message')}")
    return body["data"]


def tiktok_channel():
    """(organization_id, channel_id) of the connected TikTok channel."""
    # The Buffer account also holds the BuriedCasefiles TikTok (youtube-automation repo),
    # so "the first TikTok channel" is ambiguous: skip channels that belong to another
    # project, and refuse rather than guess if more than one candidate is left.
    orgs = _gql("query { account { organizations { id name } } }")["account"]["organizations"]
    want = os.environ.get("BUFFER_CHANNEL_ID")
    hits = []
    for org in orgs:
        chans = _gql("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
                     "{ id name displayName externalLink service isQueuePaused } }",
                     {"o": org["id"]})["channels"]
        for c in chans:
            ident = re.sub(r"[^a-z0-9]", "", f"{c['name']}{c['displayName']}{c['externalLink']}".lower())
            if (want and c["id"] == want) or (
                    not want and c["service"] == "tiktok"
                    and not any(h in ident for h in OTHER_PROJECT_HANDLES)):
                hits.append((org["id"], c))
    if len(hits) != 1:
        raise PublishError(f"expected 1 Escape Cup TikTok channel in Buffer, found {len(hits)}: "
                           f"{[c['name'] for _, c in hits]} - set BUFFER_CHANNEL_ID")
    org_id, c = hits[0]
    if c.get("isQueuePaused"):
        raise PublishError(f"Buffer queue for {c['name']} is paused")
    return org_id, c["id"]


def pending_posts(org_id, channel_id):
    data = _gql("""query($o: OrganizationId!, $c: [ChannelId!]) {
      posts(first: 20, input: {organizationId: $o, filter: {status: [scheduled], channelIds: $c}}) {
        edges { node { id dueAt text } } } }""", {"o": org_id, "c": [channel_id]})
    return [e["node"] for e in data["posts"]["edges"]]


def recent_posts(org_id, channel_id, statuses=("sent", "error"), first=10):
    data = _gql("""query($o: OrganizationId!, $c: [ChannelId!], $s: [PostStatus!]) {
      posts(first: %d, input: {organizationId: $o, filter: {status: $s, channelIds: $c}}) {
        edges { node { id dueAt status text error { message } } } } }""" % first,
                {"o": org_id, "c": [channel_id], "s": list(statuses)})
    return [e["node"] for e in data["posts"]["edges"]]


def schedule_tiktok(channel_id, video_url, caption, due_at, thumb_ms=2500):
    now = datetime.now(timezone.utc)
    mode = "customScheduled" if (due_at - now).total_seconds() > 300 else "shareNow"
    inp = {
        "text": caption,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": mode,
        "needsApproval": False,
        "assets": [{"video": {"url": video_url, "metadata": {"thumbnailOffset": thumb_ms}}}],
        # Physics simulation, not generative AI - no AIGC label.
        "metadata": {"tiktok": {"isAiGenerated": False}},
    }
    if mode == "customScheduled":
        inp["dueAt"] = due_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    data = _gql("""mutation($i: CreatePostInput!) { createPost(input: $i) {
        ... on PostActionSuccess { post { id status dueAt } }
        ... on MutationError { message } } }""", {"i": inp})
    res = data["createPost"]
    if "post" not in res:
        raise PublishError(f"Buffer rejected the post: {res.get('message')}")
    print(f"[buffer] {mode}: {res['post']}")
    return res["post"]


def delete_post(post_id):
    data = _gql("""mutation($i: DeletePostInput!) { deletePost(input: $i) {
        __typename ... on MutationError { message } } }""", {"i": {"id": post_id}})
    res = data["deletePost"]
    if res.get("message"):
        raise PublishError(f"Buffer refused to delete {post_id}: {res['message']}")
    print(f"[buffer] deleted {post_id}")


def get_post(post_id):
    return _gql("""query($i: PostInput!) { post(input: $i) {
        id status dueAt sentAt externalLink schedulingType notificationStatus
        error { message rawError supportUrl } } }""", {"i": {"id": post_id}})["post"]


def wait_until_published(post_id, timeout=900, every=20):
    """Poll Buffer until TikTok has the post (sent) or it failed. Returns the final post."""
    deadline = time.time() + timeout
    post = get_post(post_id)
    while time.time() < deadline and post["status"] in ("scheduled", "sending"):
        print(f"[buffer] {post_id}: {post['status']}")
        time.sleep(every)
        post = get_post(post_id)
    print(f"[buffer] {post_id}: final {post['status']} {post.get('externalLink') or ''}")
    return post
