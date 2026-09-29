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
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import requests

BUFFER_API = "https://api.buffer.com"
KEEP = 8


class PublishError(RuntimeError):
    pass


# ---------------------------------------------------------------- hosting --

def _git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise PublishError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def pages_base():
    repo = os.environ["GITHUB_REPOSITORY"]            # owner/name
    owner, name = repo.split("/")
    return f"https://{owner.lower()}.github.io/{name}"


def host_video(path, filename):
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
        # carry over the most recent videos (Buffer may still need them)
        if subprocess.run(["git", "fetch", "-q", "--depth", "1", remote, "media"], cwd=tmp,
                          capture_output=True).returncode == 0:
            subprocess.run(["git", "checkout", "-q", "FETCH_HEAD", "--", "."], cwd=tmp,
                           capture_output=True)
        old = sorted(f for f in os.listdir(os.path.join(tmp, "v")) if f.endswith(".mp4"))
        for f in old[: max(0, len(old) - (KEEP - 1))]:
            os.remove(os.path.join(tmp, "v", f))
        shutil.copy(path, os.path.join(tmp, "v", filename))
        open(os.path.join(tmp, ".nojekyll"), "w").close()
        with open(os.path.join(tmp, "index.html"), "w") as fh:
            fh.write("<!doctype html><title>Escape Cup media</title>")
        _git(["add", "-A"], tmp)
        _git(["-c", "user.name=github-actions[bot]",
              "-c", "user.email=github-actions[bot]@users.noreply.github.com",
              "commit", "-q", "-m", f"media: {filename}"], tmp)
        _git(["push", "-q", "-f", remote, "media"], tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    url = f"{pages_base()}/v/{filename}"
    size = os.path.getsize(path)
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            r = requests.head(url, timeout=20, allow_redirects=False)
            if r.status_code == 200 and int(r.headers.get("content-length", 0)) == size:
                print(f"[host] live: {url}")
                return url
        except requests.RequestException:
            pass
        time.sleep(15)
    raise PublishError(f"Pages never served {url} (is Pages enabled on the media branch?)")


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
    orgs = _gql("query { account { organizations { id name } } }")["account"]["organizations"]
    want = os.environ.get("BUFFER_CHANNEL_ID")
    for org in orgs:
        chans = _gql("query($o: OrganizationId!) { channels(input: {organizationId: $o}) "
                     "{ id name service isQueuePaused } }", {"o": org["id"]})["channels"]
        for c in chans:
            if (want and c["id"] == want) or (not want and c["service"] == "tiktok"):
                if c.get("isQueuePaused"):
                    raise PublishError(f"Buffer queue for {c['name']} is paused")
                return org["id"], c["id"]
    raise PublishError("no TikTok channel connected in Buffer")


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
