"""
Upload gate: the last check before any video leaves the machine (growth-lab
MASTER, vendored into each bot like growth_stats / experiment_loop. Edit here,
bump GATE_VERSION, then roll out with `py sync_vendored.py --bot <name>` - never a plain
copy, which would wipe a bot-local patch).

    verdict = upload_gate.check(path, "tiktok", profile=PROFILE, caption=..., script=...)
    if not verdict["ok"]: ...alert, do not post...
    ...
    upload_gate.require(path, "tiktok")      # INSIDE the uploader, right before the upload

Four layers, all fail-closed (a check that cannot run is a failure):
  1. platform  the platforms' published hard limits (sources below): container,
               codecs, size, frame rate, duration, vertical frame, title /
               caption / description / tag lengths, characters the API rejects.
  2. policy    the text that ships with the video (title, caption, description,
               tags) against the rules both platforms list as spam / FYF-ineligible:
               engagement bait and false incentives, off-platform links, personal
               data, misleading claims, repeats of recent posts, AI disclosure.
  3. quality   the file decodes cleanly at the expected size, frame rate and
               bitrate; no black or frozen stretches; the first frame is not
               blank; the picture changes; sound is present at a sane loudness,
               not clipped, no dead air.
  4. coherence speech matches the script (faster-whisper), and an AI review
               (Groq's free vision model) looks at frames + transcript + caption
               and answers: coherent? good enough? any guideline problem?
               Profile "ai_review": "required" blocks when the review cannot run;
               "if_available" only blocks on a negative review.

require() refuses unless THIS exact file (sha256) was approved by check() in the
same process, so no code path can upload a file the gate never saw, and a file
changed after approval is refused. There is no bypass flag.

Sources (read 2026-10-16):
  TikTok Content Posting API media transfer guide: MP4/WebM/MOV, H.264/H.265/
    VP8/VP9, 23-60 fps, 360-4096 px each side, <=10 min, <=4 GB; caption
    <=2200 UTF-16 units (Content Posting API reference).
  TikTok For You feed eligibility standards: FYF-ineligible = "like-for-like"
    promises or other false incentives, unoriginal/low-quality or watermarked
    reposts, mildly graphic material, unverified claims, undisclosed marketing.
  YouTube: Shorts are vertical or square and <=3 min; API title <=100 chars,
    description <=5000 bytes, no "<" or ">" in either, tags <=500 chars;
    >60 hashtags = all ignored. Spam policy: engagement manipulation ("sub-for-
    sub", rewards for likes), off-platform diversion, mass-produced repetitive
    content, malicious clickbait. GenAI disclosure for realistic AI content.
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher

# Bump on EVERY master edit: the ledger's gate_version attributes block-rate changes to gate
# edits, and sync_vendored.py writes it into each bot's vendored.lock.
# 2026-10-23 (S1): staged 413 shrink to fewer images, ai["retryable"], per-bot lufs windows,
#   head_similarity (warn), doubled-word lint, #fyp/#foryou fail, privacy rule, public-ledger scrub.
GATE_VERSION = "2026-10-23"

PLATFORMS = {
    "tiktok": {
        "name": "TikTok",
        "containers": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "webm", "matroska"},
        "video_codecs": {"h264", "hevc", "vp8", "vp9"},
        "fps": (23.0, 60.0),
        "px": (360, 4096),
        "seconds": (3.0, 600.0),
        "max_bytes": 4 * 1024 ** 3,
        "caption_utf16": 2200,
        "max_hashtags": 10,          # ours: no official cap, but a wall of tags reads as spam
    },
    "youtube_shorts": {
        "name": "YouTube Shorts",
        "containers": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "webm", "matroska"},
        "video_codecs": {"h264", "hevc", "vp9", "av1"},
        "fps": (23.0, 60.0),
        "px": (240, 4096),
        "seconds": (1.0, 180.0),
        "max_bytes": 256 * 1024 ** 3,
        "title_chars": 100,
        "description_bytes": 5000,
        "tags_chars": 500,
        "max_hashtags": 15,          # ours: YouTube ignores ALL hashtags above 60
    },
}

# Defaults for a 1080x1920 short; each bot passes its own overrides.
DEFAULT_PROFILE = {
    "width": 1080, "height": 1920,
    "fps": None,                     # exact fps expected (None = platform range only)
    "seconds": None,                 # (min, max) for this format
    "min_video_kbps": 700,
    "audio": True,
    "lufs": (-26.0, -9.0),
    "max_true_peak": 0.5,            # dBTP; above this the mix is clipped
    "max_silence": 4.0,              # seconds of dead air (silencedetect -50 dB)
    "max_black": 0.4,                # seconds of black in one stretch
    "max_freeze": 6.0,               # seconds of identical picture in one stretch
    "min_first_frame_std": 6.0,      # a uniform first frame = nothing for the swipe to see
    "min_changes": 3,                # of the 8 sampled gaps, at least this many must differ
    "speech_min": None,              # e.g. 0.7 = transcript must cover 70% of the script
    "whisper_model": "tiny",
    "ai_review": "off",              # "off" | "if_available" | "required"
    "ai_daily_cap": None,            # max real AI reviews per UTC day for this bot (needs "ledger")
    "ai_context": "",                # one paragraph: what the channel posts and why
    "ai_realistic": False,           # realistic AI voice/visuals -> platform AI label required
    "allowed_domains": (),           # links allowed in descriptions (YouTube only)
    "ledger": None,                  # path of the JSON audit log (None = no log)
    "bot": None,                     # a BOT_PROFILES name: its windows apply under the bot's own overrides
    "head_similarity": "warn",       # "off" | "warn" | "fail": near-duplicate first line vs the last 10
    "head_similarity_max": 0.8,      # difflib ratio on masked heads at or above this = near-duplicate
    "head_mask": (),                 # recurring proper nouns to mask before comparing ("Magnus Carlsen")
}

# Per-bot windows (MASTER_PLAN #13, plan-infra action 6). DEFAULT_PROFILE stays the wide safety
# net shared by everyone; a bot opts in with profile={"bot": "<name>", ...} AFTER its two-pass
# loudnorm ships, or the tighter window blocks every render. A "lufs" the bot passes still wins.
BOT_PROFILES = {
    "chess": {"lufs": (-16.0, -12.0)},       # loudnorm target -14
    "escape": {"lufs": (-17.0, -15.0)},      # loudnorm target -16
    "space": {"lufs": (-16.0, -12.0)},
    "quiz": {"lufs": (-16.0, -12.0)},
    "bcf": {},                               # keep the default until 3 posts are measured (ebur128)
}


def profile_for(profile):
    """DEFAULT_PROFILE <- BOT_PROFILES[profile["bot"]] <- the bot's own keys."""
    profile = dict(profile or {})
    bot = profile.get("bot")
    if bot is not None and bot not in BOT_PROFILES:
        raise ValueError(f"unknown bot profile {bot!r} (known: {', '.join(sorted(BOT_PROFILES))})")
    return {**DEFAULT_PROFILE, **BOT_PROFILES.get(bot, {}), **profile}


def bot_profile(bot, **overrides):
    """The profile dict a bot passes to check(): bot_profile("chess", ai_review="if_available")."""
    profile_for({"bot": bot})                      # validates the name
    return {"bot": bot, **overrides}


AI_MODEL = os.environ.get("UPLOAD_GATE_AI_MODEL", "qwen/qwen3.8-27b")
AI_URL = "https://api.groq.com/openai/v1/chat/completions"
# Groq's vision docs (console.groq.com/docs/vision, read 2026-10-23): for qwen/qwen3.8-27b "each
# image counts as 2048 input tokens", max 3 images. The free tier's ITPM (7000) is also a hard
# per-request cap: a bigger request gets HTTP 413 "Limit 7000, Requested N", and no wait fixes it.
# BCF's request was 3 x 2048 + ~875 text = 7019, so only FEWER images can make it fit.
AI_IMAGE_TOKENS = 2048
AI_INPUT_LIMIT = 7000                # assumed when a 413 doesn't state its limit
AI_LIMIT_MARGIN = 150                # headroom under the limit for Groq's own text-token estimate
AI_SCRIPT_CHARS = 2500
# Review shapes. Normal reviews always use stage 0 (full-size frames catch garbled on-screen text,
# e.g. "NORLANEUND"). Only after a 413 does the gate move to the first later stage predicted to
# fit: trim the text first, then fewer images. Each later stage keeps all 12 frames.
AI_STAGES = (
    {"name": "full", "script": 1.0, "tags": True,
     "first": (360, 640), "sheets": 2, "cols": 3, "rows": 2, "tile": (216, 384), "q": 5},
    {"name": "trim text", "script": 0.6, "tags": False,
     "first": (360, 640), "sheets": 2, "cols": 3, "rows": 2, "tile": (216, 384), "q": 5},
    {"name": "fewer frames", "script": 0.6, "tags": False,
     "first": (288, 512), "sheets": 1, "cols": 4, "rows": 3, "tile": (180, 320), "q": 6},
    {"name": "one image", "script": 0.6, "tags": False,
     "first": None, "sheets": 1, "cols": 4, "rows": 3, "tile": (180, 320), "q": 6},
)

# ---------------------------------------------------------------- policy text --
# Each pattern names the rule it enforces. Lower-case text is matched.
BAIT = [
    (r"\blike\s*(?:for|4)\s*like\b|\bl4l\b", "like-for-like promise (TikTok FYF: fake engagement)"),
    (r"\bfollow\s*(?:for|4)\s*follow\b|\bf4f\b", "follow-for-follow (fake engagement)"),
    (r"\bsub(?:scribe)?\s*(?:for|4)\s*sub(?:scribe)?\b|\bsub4sub\b", "sub-for-sub (YouTube spam policy)"),
    (r"\bgive\s?away\b|\bfree (?:gift|prize|robux|v-?bucks|followers|likes|money)\b|\bwin (?:a|an|free)\b",
     "giveaway / reward for engaging (false incentive)"),
    (r"\b(?:like|follow|subscribe|share|comment)\b[^.\n]{0,30}\b(?:to win|for a chance|to unlock|to enter)\b",
     "reward for engaging (false incentive)"),
    (r"\b(?:share|send) (?:this )?(?:to|with) \d+ (?:friends|people)\b|\bbad luck\b[^.\n]{0,40}\bshare\b",
     "chain-letter coercion"),
    (r"\bif you (?:don'?t|do not) (?:like|share|follow|subscribe)\b", "coercive call to action"),
]
MISLEADING = [
    (r"\bnot clickbait\b|\b100% (?:real|true|legit)\b|\bgone wrong\b|\byou won'?t believe\b",
     "clickbait phrase (misleading metadata)"),
    (r"\b(?:guaranteed|proven) to\b", "unverifiable promise"),
]
SPAM_TAGS = {"#fyp", "#foryou", "#foryoupage", "#fypシ", "#viral", "#xyzbca", "#trending", "#blowthisup"}
# These fail (S1): no reach benefit (research-algorithm-policy: 2-4 specific tags, no #fyp) and
# they read as spam. The rest of SPAM_TAGS only warn.
SPAM_FAIL_TAGS = {"#fyp", "#foryou", "#foryoupage", "#fypシ"}
# "Peter Bergmann case case." shipped on BCF. Letters only (numbers repeat legitimately), same
# line only (a caption's line break is not a doubled word). Python's re.I makes \1 case-blind.
DOUBLED_RE = re.compile(r"\b([^\W\d_]+)[ \t]+\1\b", re.I)
DOUBLED_OK = {"had", "that", "bye", "ha", "haha", "no", "so", "very", "go", "knock", "tsk", "cha", "na",
              "la", "yeah", "really", "far", "more", "again", "round", "over", "on", "and"}
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+|\b[a-z0-9-]+\.(?:com|net|org|io|co|uk|ly|gg|tv|me|app|xyz)/\S*",
                     re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
PHONE_RE = re.compile(r"(?:\+44\s?7\d{3}|\b07\d{3})\s?\d{3}\s?\d{3}\b|\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
HASHTAG_RE = re.compile(r"(?<![\w#])#\w+")

POLICY_BRIEF = {
    "tiktok": (
        "TikTok Community Guidelines + For You feed eligibility: no gore, blood, dead bodies, "
        "injuries or distressing/shocking imagery; no sexual content; nothing that sexualises or "
        "endangers minors; no hate or slurs; no dangerous acts; "
        "for real crimes only court-established facts and never implying guilt of anyone acquitted, "
        "uncharged or unconvicted; no engagement bait or false incentives; no visible watermarks or "
        "logos of other creators or stock sites (unoriginal content); no private personal data; "
        "nothing misleading in the caption about what the video shows; realistic AI content labelled."),
    "youtube_shorts": (
        "YouTube Community Guidelines + spam policy: no violent/graphic content, harassment, hate, "
        "child-safety issues or dangerous acts; title, description and visuals must match what the "
        "video actually shows (no malicious clickbait or misleading metadata); no engagement "
        "manipulation; no repetitive mass-produced low-effort content; no reuploaded third-party "
        "content or watermarks; realistic AI content disclosed."),
}


class GateBlocked(RuntimeError):
    """Raised by require() when a file was not approved by check()."""


_APPROVED = {}          # sha256 -> platform, for this process only


# ------------------------------------------------------------------- helpers --
def ffmpeg_bin():
    for c in (os.environ.get("FFMPEG_BIN"), shutil.which("ffmpeg"),
              r"C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"):
        if c and os.path.exists(c):
            return c
    return "ffmpeg"


def ffprobe_bin():
    ff = ffmpeg_bin()
    d, name = os.path.split(ff)
    cand = os.path.join(d, name.replace("ffmpeg", "ffprobe")) if d else ""
    return cand if cand and os.path.exists(cand) else (shutil.which("ffprobe") or "ffprobe")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(args, timeout=600):
    return subprocess.run(args, capture_output=True, timeout=timeout)


def probe(path):
    r = _run([ffprobe_bin(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path], 120)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.decode(errors='replace')[-300:]}")
    return json.loads(r.stdout.decode(errors="replace"))


def _norm_space(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _utf16_len(s):
    return len((s or "").encode("utf-16-le")) // 2


def mask_head(s, mask=()):
    """A headline with what legitimately changes post to post masked out: numbers, punctuation,
    emoji/flags, and the bot's recurring proper nouns (`mask`), so templates compare equal."""
    t = unicodedata.normalize("NFKD", s or "").lower()
    for term in mask or ():
        if term:
            t = re.sub(r"(?<!\w)" + re.escape(unicodedata.normalize("NFKD", term).lower()) + r"(?!\w)", "*", t)
    t = re.sub(r"\d+(?:[.,:]\d+)*", "#", t)
    t = re.sub(r"[^\w#* ]+", " ", t).replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def head_similarity(head, recent, mask=()):
    """Highest difflib ratio (0-1) between the masked head and any of the masked recent heads."""
    h = mask_head(head, mask)
    if not h:
        return 0.0
    best = max((SequenceMatcher(None, h, mask_head(r, mask), autojunk=False).ratio()
                for r in recent or () if r), default=0.0)
    return round(best, 3)


# ----------------------------------------------------------------- layer 1+2 --
def check_text(platform, *, title="", caption="", description="", tags=(), recent_texts=(),
               allowed_domains=(), ai_realistic=False, ai_label=None,
               similar_texts=None, similarity="warn", similarity_max=0.8, head_mask=()):
    """Platform limits + policy rules for the words that ship with the video.
    `similar_texts` (default: the last 10 of recent_texts) are the heads the near-duplicate
    check compares against; `similarity` is "off" | "warn" | "fail".
    Returns (failures, warnings)."""
    P = PLATFORMS[platform]
    fail, warn = [], []
    tags = [t for t in (tags or []) if t]
    if platform == "youtube_shorts":
        if not title.strip():
            fail.append("empty title")
        if len(title) > P["title_chars"]:
            fail.append(f"title is {len(title)} chars (YouTube max {P['title_chars']})")
        if len(description.encode("utf-8")) > P["description_bytes"]:
            fail.append(f"description is {len(description.encode('utf-8'))} bytes (YouTube max 5000)")
        for name, txt in (("title", title), ("description", description)):
            if "<" in txt or ">" in txt:
                fail.append(f"'<' or '>' in the {name} (YouTube API rejects it)")
        tag_len = sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)
        if tag_len > P["tags_chars"]:
            fail.append(f"tags total {tag_len} chars (YouTube max 500)")
        texts = {"title": title, "description": description, "tags": " ".join(tags)}
    else:
        if not caption.strip():
            fail.append("empty caption")
        if _utf16_len(caption) > P["caption_utf16"]:
            fail.append(f"caption is {_utf16_len(caption)} UTF-16 units (TikTok max 2200)")
        texts = {"caption": caption}

    allt = "\n".join(texts.values())
    low = allt.lower()
    for pat, why in BAIT + MISLEADING:
        m = re.search(pat, low)
        if m:
            fail.append(f"{why}: \"{m.group(0)}\"")
    for m in EMAIL_RE.finditer(allt):
        fail.append(f"email address in the text ({m.group(0)}) - personal data")
    for m in PHONE_RE.finditer(allt):
        fail.append(f"phone number in the text ({m.group(0)}) - personal data")
    for m in URL_RE.finditer(allt):
        url = m.group(0)
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", url.lower()).split("/")[0]
        if platform == "tiktok":
            fail.append(f"link in a TikTok caption ({url}) - off-platform diversion")
        elif not any(host == d or host.endswith("." + d) for d in allowed_domains):
            fail.append(f"link to an unapproved site ({host}) - off-platform diversion risk")
    hashtags = HASHTAG_RE.findall(allt)
    if len(hashtags) > P["max_hashtags"]:
        fail.append(f"{len(hashtags)} hashtags (max {P['max_hashtags']}; excess hashtags read as spam)")
    low_tags = [h.lower() for h in hashtags]
    spam_fail = sorted(set(low_tags) & SPAM_FAIL_TAGS)
    if spam_fail:
        fail.append(f"generic reach hashtags {' '.join(spam_fail)} - remove them (no reach benefit, "
                    "they read as spam; use 2-4 specific tags)")
    spam = sorted(set(low_tags) & (SPAM_TAGS - SPAM_FAIL_TAGS))
    if spam:
        warn.append(f"generic reach hashtags {' '.join(spam)} (no reach benefit; can read as spam)")
    if platform == "tiktok" and len(hashtags) > 5:
        warn.append(f"{len(hashtags)} hashtags - TikTok search uses the first few")

    # Doubled words ("case case") in the words a viewer reads; tags are a list, not prose.
    for name, txt in texts.items():
        if name == "tags":
            continue
        for m in DOUBLED_RE.finditer(txt):
            if m.group(1).lower() not in DOUBLED_OK:
                fail.append(f"doubled word in the {name}: \"{m.group(0)}\" (template bug)")

    # Repetition: the same headline as a recent post is the "repetitive content" signal.
    raw_head = title if platform == "youtube_shorts" else caption.split("\n")[0]
    head = _norm_space(raw_head)
    what = "title" if platform == "youtube_shorts" else "first caption line"
    recent = {_norm_space(t) for t in recent_texts if t}
    if head and head in recent:
        fail.append(f"same {what} as a recent post (repetitive content): \"{head[:80]}\"")
    elif head and similarity != "off":
        pool = list(recent_texts)[-10:] if similar_texts is None else list(similar_texts)
        sim = head_similarity(raw_head, pool, head_mask)
        if sim >= similarity_max:
            msg = (f"near-duplicate {what} (similarity {sim:.2f} >= {similarity_max} to a recent post; "
                   f"templated heads read as repetitive content): \"{head[:80]}\"")
            (fail if similarity == "fail" else warn).append(msg)

    if ai_realistic and ai_label is not True:
        fail.append(f"realistic AI content without the {P['name']} AI label")
    return fail, warn


# ------------------------------------------------------------------- layer 3 --
def _frame(path, t, w=270, h=480):
    import numpy as np
    raw = _run([ffmpeg_bin(), "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path,
                "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
               120).stdout
    if len(raw) != w * h * 3:
        raise RuntimeError(f"could not decode a frame at {t:.1f}s")
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.float32)


def check_media(path, platform, profile):
    """Platform file limits + technical quality. Returns (report, failures, warnings)."""
    P, pr = PLATFORMS[platform], profile
    rep, fail, warn = {}, [], []
    info = probe(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return rep, ["no video stream"], warn
    fmt = info.get("format", {})
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    size = int(fmt.get("size") or os.path.getsize(path))
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    vkbps = (int(v.get("bit_rate") or 0) / 1000) or (size * 8 / max(dur, 0.01) / 1000)
    rep.update(res=f"{w}x{h}", fps=round(fps, 2), seconds=round(dur, 2), mb=round(size / 1e6, 1),
               vcodec=v.get("codec_name"), acodec=(a or {}).get("codec_name"), kbps=round(vkbps))

    # --- platform hard limits
    containers = set((fmt.get("format_name") or "").split(","))
    if not containers & P["containers"]:
        fail.append(f"container {fmt.get('format_name')} not accepted by {P['name']}")
    if v.get("codec_name") not in P["video_codecs"]:
        fail.append(f"video codec {v.get('codec_name')} not accepted by {P['name']}")
    if not P["fps"][0] - 0.5 <= fps <= P["fps"][1] + 0.5:
        fail.append(f"{fps:.2f} fps outside {P['name']}'s {P['fps'][0]:.0f}-{P['fps'][1]:.0f}")
    if not (P["px"][0] <= min(w, h) and max(w, h) <= P["px"][1]):
        fail.append(f"{w}x{h} outside {P['name']}'s {P['px'][0]}-{P['px'][1]} px")
    if h < w:
        fail.append(f"{w}x{h} is landscape - {P['name']} needs vertical")
    if not P["seconds"][0] <= dur <= P["seconds"][1]:
        fail.append(f"{dur:.1f}s outside {P['name']}'s {P['seconds'][0]:.0f}-{P['seconds'][1]:.0f}s")
    if size > P["max_bytes"]:
        fail.append(f"file is {size / 1e9:.1f} GB (over {P['name']}'s limit)")
    if v.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
        fail.append(f"pixel format {v.get('pix_fmt')} (phones need yuv420p)")

    # --- this format's own spec
    if pr.get("width") and (w, h) != (pr["width"], pr["height"]):
        fail.append(f"resolution {w}x{h}, expected {pr['width']}x{pr['height']}")
    if pr.get("fps") and abs(fps - pr["fps"]) > 0.5:
        fail.append(f"{fps:.2f} fps, expected {pr['fps']}")
    if pr.get("seconds") and not pr["seconds"][0] <= dur <= pr["seconds"][1]:
        fail.append(f"{dur:.1f}s outside this format's {pr['seconds'][0]}-{pr['seconds'][1]}s")
    if vkbps < pr["min_video_kbps"]:
        fail.append(f"video bitrate {vkbps:.0f} kbps < {pr['min_video_kbps']} (comes out blurry after re-encode)")
    if pr.get("audio") and not a:
        fail.append("no audio stream")

    # --- one full decode: corruption, black, freeze, loudness, silence
    vf = (f"blackdetect=d={min(pr['max_black'], 0.2):.2f}:pix_th=0.05,"
          f"freezedetect=n=0.001:d={max(0.5, pr['max_freeze']):.2f}")
    args = [ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "info", "-i", path, "-vf", vf]
    if a:
        args += ["-af", f"ebur128=peak=true,silencedetect=n=-50dB:d={pr['max_silence']:.2f}"]
    r = _run(args + ["-f", "null", "-"], 900)
    log = r.stderr.decode(errors="replace")
    if r.returncode != 0:
        fail.append(f"ffmpeg could not decode the file: {log.strip().splitlines()[-1][:160] if log.strip() else '?'}")
    errs = [ln for ln in log.splitlines() if re.search(r"\b(error|corrupt|invalid data|non-existing PPS)\b", ln, re.I)
            and "error concealment" not in ln.lower()]
    rep["decode_errors"] = len(errs)
    if errs:
        fail.append(f"{len(errs)} decode error(s), e.g. {errs[0].strip()[:120]}")
    blacks = [float(x) for x in re.findall(r"black_duration:([\d.]+)", log)]
    rep["longest_black"] = max(blacks, default=0.0)
    if rep["longest_black"] > pr["max_black"]:
        fail.append(f"{rep['longest_black']:.1f}s of black picture")
    freezes = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", log)]
    # a freeze still running at the end is reported without a duration
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", log)]
    if len(starts) > len(freezes):
        freezes.append(dur - starts[-1])
    rep["longest_freeze"] = round(max(freezes, default=0.0), 2)
    if rep["longest_freeze"] > pr["max_freeze"]:
        fail.append(f"picture frozen for {rep['longest_freeze']:.1f}s")
    if a:
        lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
        peak = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", log)
        rep["lufs"] = float(lufs[-1]) if lufs else None
        rep["true_peak"] = float(peak[-1]) if peak and peak[-1] != "-inf" else None
        if rep["lufs"] is None:
            fail.append("could not measure loudness")
        elif not pr["lufs"][0] <= rep["lufs"] <= pr["lufs"][1]:
            fail.append(f"loudness {rep['lufs']} LUFS outside {pr['lufs']}")
        if rep["true_peak"] is not None and rep["true_peak"] > pr["max_true_peak"]:
            fail.append(f"true peak {rep['true_peak']} dBTP - clipped")
        sil = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", log)]
        s_starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
        if len(s_starts) > len(sil):
            sil.append(dur - s_starts[-1])
        rep["longest_silence"] = round(max(sil, default=0.0), 2)
        if rep["longest_silence"] > pr["max_silence"]:
            fail.append(f"{rep['longest_silence']:.1f}s of dead silence")

    # --- the picture itself: first frame visible, video actually moves
    times = [0.05] + [dur * (i + 0.5) / 8 for i in range(8)]
    frames = [_frame(path, t) for t in times]
    rep["first_frame_std"] = round(float(frames[0].std()), 1)
    if rep["first_frame_std"] < pr["min_first_frame_std"]:
        fail.append("first frame is blank - nothing for the viewer to see before swiping")
    diffs = [float(abs(frames[i + 1] - frames[i]).mean()) for i in range(1, len(frames) - 1)]
    rep["changes"] = sum(d > 2.0 for d in diffs)
    if rep["changes"] < pr["min_changes"]:
        fail.append(f"picture barely changes ({rep['changes']}/{len(diffs)} sampled gaps differ) - looks stuck")
    return rep, fail, warn


# ------------------------------------------------------------------- layer 4 --
_NUM_WORDS = set(("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                  "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty "
                  "sixty seventy eighty ninety hundred thousand million billion and first second "
                  "third fourth fifth sixth seventh eighth ninth tenth oh").split())


def _words(text):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[-‐-―/]", " ", t)
    t = re.sub(r"[^a-z0-9' ]+", " ", t).replace("'", "")
    # numbers are spoken and transcribed in too many forms to compare; drop them
    return [w for w in t.split() if not w.isdigit() and w not in _NUM_WORDS and not re.fullmatch(r"\d+\w*", w)]


def transcribe(path, model="tiny"):
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(path, language="en", beam_size=1, vad_filter=False,
                           condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs)


def speech_match(script, transcript):
    """Share of the script's words heard in order in the transcript (0-1)."""
    a, b = _words(script), _words(transcript)
    if not a:
        return 1.0
    sm = SequenceMatcher(None, a, b, autojunk=False)
    return sum(blk.size for blk in sm.get_matching_blocks()) / len(a)


def _images_b64(path, dur, sheets=2, cols=3, rows=2, tile=(216, 384), first=(360, 640), q=5):
    """[first frame at `first` (skipped when None), then `sheets` contact sheets of cols*rows
    frames spread evenly through the video, left-to-right, top-to-bottom]. The model takes <=3
    images, and Groq counts each as AI_IMAGE_TOKENS input tokens whatever its size."""
    import tempfile
    out, names = [], []
    with tempfile.TemporaryDirectory() as tmp:
        if first:
            _run([ffmpeg_bin(), "-loglevel", "error", "-ss", "0.3", "-i", path, "-frames:v", "1",
                  "-vf", f"scale={first[0]}:{first[1]}", "-q:v", str(q), os.path.join(tmp, "first.jpg")], 120)
            names.append("first.jpg")
        n = sheets * cols * rows
        _run([ffmpeg_bin(), "-loglevel", "error", "-i", path, "-vf",
              f"fps={n / max(dur, 1):.5f},scale={tile[0]}:{tile[1]},tile={cols}x{rows}", "-frames:v", str(sheets),
              "-q:v", str(q), os.path.join(tmp, "sheet%d.jpg")], 300)
        names += [f"sheet{i}.jpg" for i in range(1, sheets + 1)]
        for name in names:
            p = os.path.join(tmp, name)
            if not os.path.exists(p) or not os.path.getsize(p):
                raise RuntimeError(f"could not grab frames ({name})")
            with open(p, "rb") as fh:
                out.append(base64.b64encode(fh.read()).decode())
    return out


def _redact(s):
    """Groq error bodies name the account's organisation id; keep it out of public logs."""
    return re.sub(r"org_[A-Za-z0-9]+", "org_***", s)


def _stage_images(stage):
    return (1 if stage["first"] else 0) + stage["sheets"]


def _ai_prompt(platform, profile, dur, title, caption, transcript, stage):
    """The reviewer's instructions for one review shape (AI_STAGES)."""
    per_sheet, sheets = stage["cols"] * stage["rows"], stage["sheets"]
    frames = per_sheet * sheets
    if stage["first"] and sheets > 1:
        images = (f"Image 1 is the first frame (what a scroller sees). Images 2-{1 + sheets} are contact sheets "
                  f"of {frames} frames")
    elif stage["first"]:
        images = f"Image 1 is the first frame (what a scroller sees). Image 2 is a contact sheet of {frames} frames"
    else:
        images = (f"Image 1 is a contact sheet of {frames} frames (its first tile is the opening frame, "
                  "what a scroller sees)")
    images += f" spread evenly through the {dur:.0f}s video, in reading order.\n"
    script = transcript or "(no narration)"
    full = script[:AI_SCRIPT_CHARS]
    if stage["script"] < 1 and len(full) > 300:
        keep = max(300, int(len(full) * stage["script"]))
        narration = (f"Narration script excerpt (the first {keep} of {len(script)} characters; verified to match "
                     f"the audio - it is shortened here only to fit this request, the video's narration is "
                     f"complete, so never call it cut off): {full[:keep]}")
    else:
        narration = f"Narration script (verified to match the audio): {full}"
    if not stage["tags"]:                      # trimmed stages: hashtags add tokens, not judgement
        caption = "\n".join(ln for ln in (re.sub(r"\s+", " ", HASHTAG_RE.sub("", x)).strip()
                                          for x in (caption or "").split("\n")) if ln)
    return (
        f"You are the final pre-publication reviewer for a {PLATFORMS[platform]['name']} channel. "
        # Without the date the model called 2025/2026 events "future dates" and blocked them.
        f"Today's date is {datetime.now(timezone.utc):%d %B %Y}; events up to today are in the past.\n"
        f"Channel: {profile.get('ai_context') or 'short vertical videos'}\n"
        f"{images}"
        f"Title: {title or '-'}\nCaption: {caption or '-'}\n"
        f"{narration}\n\n"
        f"Platform rules: {POLICY_BRIEF[platform]}\n\n"
        "Judge: (1) COHERENCE - do the visuals, on-screen text, narration and caption tell one "
        "consistent story that matches the title, with nothing irrelevant, contradictory, garbled, "
        "cut off or glitched? (2) QUALITY - would a viewer see this as a finished, watchable video "
        "(readable text, no broken rendering, not a placeholder)? (3) POLICY - any rule above broken?\n"
        "Ground rules: a plain request to comment, like or follow is ALLOWED (only rewards or "
        "false promises are bait). Burned-in captions show one or a few words at a time, so a "
        "lone word in one frame is normal. Do not fact-check the story: facts were verified "
        "against sources before this step; judge only what is on screen and how it fits. "
        "Never block for factual accuracy, dates or unverifiable details - that is out of scope here. Stock "
        "or mood footage is fine when its mood fits the story; flag footage that is jarringly "
        "unrelated or inappropriate for the subject (e.g. food, animals, parties, sport, "
        "celebrations in a story about a death).\n"
        # BCF 396565f (audit 2026-10-21 bcf #1): real homes / plates beside crime narration.
        "PRIVACY (always BLOCK, list it under policy_issues): any frame that shows a real "
        "private home (a house, cottage, front door or driveway of a residence) or a vehicle "
        "number plate that can be read. Viewers take a home beside crime narration as the "
        "crime scene or a suspect's house, which harms uninvolved owners.\n"
        "Only BLOCK for a concrete problem you can point to in the frames or text, not for style.\n"
        'Reply with JSON only: {"verdict":"PASS" or "BLOCK","coherence":1-10,"quality":1-10,'
        '"policy_issues":["..."],"quality_issues":["..."],"summary":"one sentence"}')


def _body_parts(body):
    content = body["messages"][0]["content"]
    return content[0]["text"], sum(1 for c in content if c.get("type") == "image_url")


def predict_tokens(body, calibrate=None):
    """Groq's input-token count for a review request: AI_IMAGE_TOKENS per image plus the text.
    calibrate=(earlier_body, its_requested_tokens) takes the text's tokens-per-character from a
    413's "Requested N" figure; without it, a conservative 1 token per 3 characters."""
    text, n_img = _body_parts(body)
    per_char = 1 / 3
    if calibrate:
        ref, requested = calibrate
        ref_text, ref_img = _body_parts(ref)
        per_char = max(1.0, requested - AI_IMAGE_TOKENS * ref_img) / max(1, len(ref_text))
    return int(-(-(n_img * AI_IMAGE_TOKENS + len(text) * per_char) // 1))


def _http_body(e):
    try:
        return _redact(e.read()[:400].decode(errors="replace"))
    except Exception:
        return ""


def _is_timeout(e):
    import socket
    reason = getattr(e, "reason", None)
    return (isinstance(e, (socket.timeout, TimeoutError)) or isinstance(reason, (socket.timeout, TimeoutError))
            or "timed out" in str(e).lower())


def _unavailable(reason, problem, retryable=False, **extra):
    """An AI review that produced no verdict. `retryable` is True ONLY when waiting can fix it
    (a 429 without "per day", a 5xx, a timeout): callers re-check on that and nothing else."""
    return {"ok": False, "available": False, "retryable": retryable, "reason": reason,
            "problems": [problem], **extra}


def ai_review(path, platform, profile, *, title="", caption="", transcript="", seconds=None):
    """Ask a free vision model to watch the video as a strict pre-publication reviewer.
    Returns {"ok": bool, "available": bool, "retryable": bool, "reason", "coherence", "quality",
    "problems": [...], "summary"}; "shrunk" when a 413 forced a smaller request.
    The prompt (_ai_prompt) carries the PRIVACY rule at every stage: a real private home or a
    readable vehicle number plate in any frame is always a BLOCK."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return _unavailable("no_key", "GROQ_API_KEY not set")
    dur = seconds or float(probe(path)["format"]["duration"])
    grabbed = {}

    def build(i):
        st = AI_STAGES[i]
        geo = {k: st[k] for k in ("sheets", "cols", "rows", "tile", "first", "q")}
        gkey = json.dumps(geo, sort_keys=True)
        if gkey not in grabbed:
            grabbed[gkey] = _images_b64(path, dur, **geo)
        content = [{"type": "text", "text": _ai_prompt(platform, profile, dur, title, caption, transcript, st)}]
        for b64 in grabbed[gkey]:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
        # Reasoning off: hidden reasoning was ~3k tokens a review, and the model's free
        # quota is 200k tokens/DAY shared by all three bots (calibration day 2026-10-16
        # exhausted it). The verdict is a visual judgement; it doesn't need a chain.
        # max_tokens stays 900 at every stage: it is the reply budget, never the 413 lever.
        return {"model": AI_MODEL, "temperature": 0, "max_tokens": 900, "reasoning_effort": "none",
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": content}]}

    stage, body = 0, build(0)
    shrunk, transient, res, tokens = None, 0, {}, None
    while True:
        try:
            req = urllib.request.Request(AI_URL, data=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "upload-gate"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                reply = json.load(resp)
            out = reply["choices"][0]["message"]["content"]
            tokens = (reply.get("usage") or {}).get("total_tokens")
            m = re.search(r"\{.*\}", out, re.S)
            try:
                res = json.loads(m.group(0)) if m else {}
            except ValueError:
                res = {}
            if res.get("verdict") not in ("PASS", "BLOCK"):
                transient += 1
                if transient < 3:
                    time.sleep(10)
                    continue
                return _unavailable("no_verdict", "AI review failed: the model gave no verdict")
            break
        except urllib.error.HTTPError as e:
            body_txt = _http_body(e)
            last = f"HTTP {e.code}: {body_txt[:200]}"
            if "per day" in body_txt:              # daily quota gone: waiting a minute won't help
                return _unavailable("quota", "AI review skipped: the free daily AI quota is used up "
                                             "(it refills over the next 24h)", quota=True)
            if e.code == 413:
                # A hard per-request input cap (Groq free tier: ITPM 7000). No wait fixes it, so
                # re-send the first later stage PREDICTED to fit under the limit the 413 states
                # (calibrated on this very request), or fail closed without another request.
                lim = re.search(r"Limit\s+(\d+)", body_txt)
                got = re.search(r"Requested\s+(\d+)", body_txt)
                limit = int(lim.group(1)) if lim else AI_INPUT_LIMIT
                # Groq's own count wins; without it, at least the limit + 1 (it was refused).
                requested = int(got.group(1)) if got else max(limit + 1, predict_tokens(body))
                cal = (body, requested)
                nxt = None
                for j in range(stage + 1, len(AI_STAGES)):
                    cand = build(j)
                    p = predict_tokens(cand, calibrate=cal)
                    if p <= limit - AI_LIMIT_MARGIN:
                        nxt = (j, cand, p)
                        break
                if nxt is None:
                    return _unavailable(
                        "too_large", f"AI review request too large ({requested} > {limit} input tokens even "
                                     f"after shrinking): {last}", too_large=True,
                        shrunk={"stage": AI_STAGES[stage]["name"], "images": _body_parts(body)[1],
                                "limit": limit, "requested": requested})
                stage, body, p = nxt
                shrunk = {"stage": AI_STAGES[stage]["name"], "images": _body_parts(body)[1], "limit": limit,
                          "requested": requested, "predicted_tokens": p}
                continue
            if e.code in (429, 500, 502, 503, 504):
                transient += 1
                if transient < 3:
                    try:
                        wait = float((e.headers or {}).get("retry-after") or 30)
                    except (TypeError, ValueError):
                        wait = 30
                    time.sleep(min(65, wait))
                    continue
                return _unavailable("rate_limited" if e.code == 429 else "server_error",
                                    f"AI review failed: {last}", retryable=True)
            return _unavailable(f"http_{e.code}", f"AI review failed: {last}")
        except Exception as e:                     # network trouble
            timeout = _is_timeout(e)
            transient += 1
            if transient < 3:
                time.sleep(10)
                continue
            return _unavailable("timeout" if timeout else "network", f"AI review failed: {str(e)[:200]}",
                                retryable=timeout)
    coh, qual = int(res.get("coherence") or 0), int(res.get("quality") or 0)
    problems = [str(p) for p in (res.get("policy_issues") or []) + (res.get("quality_issues") or []) if p]
    ok = res["verdict"] == "PASS" and coh >= 5 and qual >= 5 and not (res.get("policy_issues") or [])
    out = {"ok": ok, "available": True, "retryable": False, "reason": None, "coherence": coh, "quality": qual,
           "problems": problems[:6], "summary": str(res.get("summary") or "")[:300], "model": AI_MODEL,
           "tokens": tokens}
    if shrunk:
        out["shrunk"] = shrunk
    return out


# ---------------------------------------------------------------------- main --
def _head(platform, title, caption):
    return (title if platform == "youtube_shorts" else (caption or "").split("\n")[0]).strip()


def check(path, platform, *, profile=None, title="", caption="", description="", tags=(), script="",
          recent_texts=(), ai_label=None, extra_failures=()):
    """Run every layer. Returns the verdict dict; approves the file for require() only when ok."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform}")
    pr = profile_for(profile)                    # DEFAULT_PROFILE <- bot window <- bot's keys
    if pr.get("ledger"):                         # headlines this gate approved before count as recent
        recent_texts = list(recent_texts) + recent_heads(pr["ledger"], platform)
    head = _head(platform, title, caption)
    verdict = {"ok": False, "platform": platform, "file": os.path.basename(path), "sha256": None,
               "head": head, "head_similarity": head_similarity(head, list(recent_texts)[-10:], pr["head_mask"]),
               "failures": list(extra_failures), "warnings": [], "report": {}, "ai": None,
               "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "gate_version": GATE_VERSION}
    F, W = verdict["failures"], verdict["warnings"]

    def stage(name, fn):
        try:
            return fn()
        except Exception as exc:                 # fail-closed: a check that can't run blocks
            F.append(f"{name} check could not run: {str(exc)[:200]}")
            return None

    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        F.append("video file missing or empty")
        return _finish(verdict, pr)
    verdict["sha256"] = stage("hash", lambda: sha256(path))
    res = stage("text", lambda: check_text(
        platform, title=title, caption=caption, description=description, tags=tags,
        recent_texts=recent_texts, allowed_domains=pr["allowed_domains"],
        ai_realistic=pr["ai_realistic"], ai_label=ai_label, similarity=pr["head_similarity"],
        similarity_max=pr["head_similarity_max"], head_mask=pr["head_mask"]))
    if res:
        F.extend(res[0]); W.extend(res[1])
    res = stage("video", lambda: check_media(path, platform, pr))
    if res:
        verdict["report"].update(res[0]); F.extend(res[1]); W.extend(res[2])

    transcript = ""
    if pr.get("speech_min") and script:
        transcript = stage("speech", lambda: transcribe(path, pr["whisper_model"])) or ""
        if transcript:
            match = speech_match(script, transcript)
            verdict["report"]["speech_match"] = round(match, 2)
            if match < pr["speech_min"]:
                F.append(f"narration doesn't match the script ({match:.0%} of the script heard, "
                         f"need {pr['speech_min']:.0%}) - garbled, cut off or wrong audio")
            elif match < min(0.95, pr["speech_min"] + 0.12):
                W.append(f"narration only {match:.0%} matched to the script")

    mode = pr.get("ai_review", "off")
    cap = pr.get("ai_daily_cap")
    if mode != "off" and not F and cap and pr.get("ledger") and ai_reviews_today(pr["ledger"]) >= cap:
        # The free AI quota is shared by 3 bots; retry loops must not let one bot spend it
        # all. Same outcome as Groq's own "per day" answer, just reached earlier.
        ai = {"ok": False, "available": False, "quota": True, "cap": True, "retryable": False, "reason": "cap",
              "problems": [f"AI review skipped: this bot's daily AI-review cap ({cap}) is reached "
                           "(the free AI quota is shared by 3 bots)"]}
        verdict["ai"] = ai
        (F if mode == "required" else W).append("AI review unavailable: " + "; ".join(ai["problems"]))
    elif mode != "off" and not F:                # don't spend the AI budget on a video already blocked
        # The reviewer reads the SCRIPT, not Whisper's transcript: the speech check above
        # already proved the audio says the script, and Whisper's mishearings ("porn"
        # for "pawn") made the reviewer blame the narration for words never spoken.
        ai = stage("AI review", lambda: ai_review(
            path, platform, pr, title=title, caption=caption or description, transcript=script or transcript,
            seconds=verdict["report"].get("seconds")))
        if ai is not None:
            verdict["ai"] = ai
            if ai["available"] and not ai["ok"]:
                F.append("AI review: " + (ai.get("summary") or "blocked") + (
                    " | " + "; ".join(ai["problems"]) if ai["problems"] else ""))
            elif not ai["available"]:
                (F if mode == "required" else W).append("AI review unavailable: " + "; ".join(ai["problems"]))
    elif mode == "required" and F:
        verdict["ai"] = {"skipped": "earlier failures"}
    return _finish(verdict, pr)


def _finish(verdict, pr):
    verdict["ok"] = not verdict["failures"] and bool(verdict["sha256"])
    if verdict["ok"]:
        _APPROVED[verdict["sha256"]] = verdict["platform"]
    if pr.get("ledger"):
        try:
            record(verdict, pr["ledger"])
        except Exception as exc:
            print(f"[gate] ledger not written: {exc}")
    print(f"[gate] {'PASS' if verdict['ok'] else 'BLOCK'} {verdict['file']} ({verdict['platform']}) "
          f"{json.dumps(verdict['report'], default=str)}")
    if (verdict.get("ai") or {}).get("available"):
        print(f"[gate]   AI: {verdict['ai'].get('coherence')}/10 coherent, {verdict['ai'].get('quality')}/10 "
              f"quality - {verdict['ai'].get('summary')}")
    for f in verdict["failures"]:
        print(f"[gate]   ✗ {f}")
    for w in verdict["warnings"]:
        print(f"[gate]   ! {w}")
    return verdict


def require(path, platform):
    """Call INSIDE the uploader, immediately before the upload. Raises GateBlocked
    unless this exact file was approved for this platform in this process."""
    if not os.path.isfile(path):
        raise GateBlocked(f"upload gate: {path} does not exist")
    digest = sha256(path)
    if _APPROVED.get(digest) != platform:
        raise GateBlocked(f"upload gate: {os.path.basename(path)} was not approved for {platform} "
                          "(never checked, failed, or changed after checking)")
    return digest


# The ledger is committed to public repos. An AI reviewer's reason for a legal block
# ("implies the guilt of an uncharged suspect, <name>") restates the allegation the gate
# refused to publish (audit 2026-10-21 F12), so such text is withheld from the file.
# The in-memory verdict (alerts, retries) keeps the full text. (Folded from BCF 396565f.)
# Security review 2026-10-23: a keyword list is bypassable ("names the man next door as the
# one who did it"), so the public log is fail-closed: AI-review reasons and summaries are
# always fixed codes, a gate-generated message is kept only with its quoted / personal-data
# parts removed, and any other text (e.g. a bot's own extra_failures) becomes a fixed code.
# _ALLEGATION stays as a second net over what survives.
_ALLEGATION = re.compile(
    r"\b(suspect\w*|acquit\w*|convict\w*|uncharged|charged|charges|implicat\w*|accus\w*|guilt\w*|"
    r"persons? of interest|perpetrator\w*|culprit\w*|killers?|murderers?|wrongdoing|defam\w*|libel\w*|"
    r"arrest\w*|framed|innocen\w*|hitm[ae]n|accomplice\w*|blame\w*)\b", re.I)
WITHHELD = "[legal-risk detail withheld from the public log]"
AI_WITHHELD = "[ai_block] withheld from the public log"
OTHER_WITHHELD = "[other] detail withheld from the public log"
# Messages this module writes itself (check_text, check_media, check). Content words only ever
# appear inside "quotes" or the (parentheses) of the personal-data/link rules; those are cut.
_PUBLIC_SAFE = re.compile(
    r"^(?:empty (?:title|caption)|title is \d|description is \d|'<' or '>' in the |tags total \d|caption is \d|"
    + "|".join(re.escape(why) for _, why in BAIT + MISLEADING) +
    r"|email address in the text|phone number in the text|link in a TikTok caption|link to an unapproved site"
    r"|\d+ hashtags|generic reach hashtags|doubled word in the (?:title|caption|description)"
    r"|same (?:title|first caption line) as a recent post|near-duplicate (?:title|first caption line)"
    r"|realistic AI content without|no video stream|no audio stream|container \S+ not accepted"
    r"|video codec \S+ not accepted|[\d.]+ fps|\d+x\d+ |-?[\d.]+s outside|file is [\d.]+ GB|pixel format"
    r"|resolution \d|video bitrate|ffmpeg could not decode|\d+ decode error|[\d.]+s of black picture"
    r"|picture frozen|could not measure loudness|loudness -?[\d.]+ LUFS|true peak|[\d.]+s of dead silence"
    r"|first frame is blank|picture barely changes|video file missing or empty"
    r"|(?:hash|text|video|speech|AI review) check could not run"
    r"|narration doesn't match the script|narration only \d+% matched)")
_PERSONAL = re.compile(r"^((?:email address|phone number) in the text|link in a TikTok caption"
                       r"|link to an unapproved site) \([^)]*\)")
_UNAVAILABLE_CODES = (("too large", "too_large"), ("daily AI quota", "quota"), ("daily AI-review cap", "cap"),
                      ("not set", "no_key"), ("no verdict", "no_verdict"), ("timed out", "timeout"))
_PUBLIC_KEYS = ("checked_at", "platform", "file", "sha256", "head", "ok", "failures", "warnings",
                "gate_version", "head_similarity", "report", "ai")
_PUBLIC_REPORT = ("seconds", "lufs", "speech_match", "longest_freeze")
_PUBLIC_AI_NUM = ("available", "ok", "coherence", "quality", "tokens", "retryable", "too_large", "quota", "cap")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def scrub_public(text):
    """The public-log form of one failure/warning line: a gate-generated technical message
    (minus quoted words and personal data), else a fixed code. Never free text."""
    if not isinstance(text, str):
        return None
    if text.startswith("AI review unavailable: "):
        rest = text[len("AI review unavailable: "):]
        code = next((c for k, c in _UNAVAILABLE_CODES if k in rest), None)
        if code is None:
            m = re.search(r"\bHTTP (\d{3})\b", rest)
            code = f"http_{m.group(1)}" if m else "unavailable"
        return f"AI review unavailable: [{code}]"
    if text.startswith("AI review: "):
        return "AI review: " + AI_WITHHELD
    if not _PUBLIC_SAFE.match(text):
        return WITHHELD if _ALLEGATION.search(text) else OTHER_WITHHELD
    out = _PERSONAL.sub(r"\1 (…)", text)
    out = re.sub(r"\"[^\"]*\"?", '"…"', out)
    if _ALLEGATION.search(out):
        return WITHHELD
    return out


def _num(v):
    return v if v is None or isinstance(v, (bool, int, float)) else None


def _scrub_entry(e):
    """Allowlist of structured fields for the public ledger. Blocked entries lose `head` and
    `file` (free text a reviewer refused to publish); recent_heads reads only ok entries."""
    e = e if isinstance(e, dict) else {}
    out = {k: e.get(k) for k in _PUBLIC_KEYS}
    ok = out["ok"] is True
    out["ok"] = ok if isinstance(e.get("ok"), bool) else None
    for k in ("checked_at", "platform", "sha256", "gate_version"):
        if not isinstance(out[k], str) or not re.fullmatch(r"[\w.:+\-]{0,80}", out[k]):
            out[k] = None
    if not ok or not isinstance(out["head"], str):
        out["head"] = None
    if not ok or not isinstance(out["file"], str):
        out["file"] = None
    out["head_similarity"] = _num(out["head_similarity"])
    out["failures"] = [scrub_public(f) for f in (e.get("failures") or []) if isinstance(f, str)]
    out["warnings"] = [scrub_public(w) for w in (e.get("warnings") or []) if isinstance(w, str)]
    rep = e.get("report") if isinstance(e.get("report"), dict) else {}
    out["report"] = {k: _num(rep.get(k)) for k in _PUBLIC_REPORT}
    ai = e.get("ai")
    if isinstance(ai, dict):
        slim = {k: _num(ai.get(k)) for k in _PUBLIC_AI_NUM if k in ai}
        reason = ai.get("reason")
        slim["reason"] = reason if isinstance(reason, str) and _CODE_RE.match(reason) else None
        model = ai.get("model")
        if isinstance(model, str) and re.fullmatch(r"[\w./:\-]{1,64}", model):
            slim["model"] = model
        if ai.get("skipped") == "earlier failures":
            slim["skipped"] = "earlier failures"
        summary = ai.get("summary")
        if summary is not None:
            slim["summary"] = "[ai_pass]" if ai.get("ok") is True else AI_WITHHELD
        if isinstance(ai.get("shrunk"), dict):
            sh = {k: v for k, v in ai["shrunk"].items()
                  if k in ("images", "limit", "requested", "predicted_tokens") and isinstance(v, int)}
            if ai["shrunk"].get("stage") in {s["name"] for s in AI_STAGES}:
                sh["stage"] = ai["shrunk"]["stage"]
            slim["shrunk"] = sh
        out["ai"] = slim
    else:
        out.pop("ai")
    return out


def record(verdict, ledger_path, keep=300):
    """Append to the JSON audit log (what was checked, when, and why it passed or failed)."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {"entries": []}
    slim = {k: verdict.get(k) for k in ("checked_at", "platform", "file", "sha256", "head", "ok", "failures",
                                        "warnings", "gate_version", "head_similarity")}
    slim["report"] = {k: verdict["report"].get(k) for k in ("seconds", "lufs", "speech_match", "longest_freeze")}
    if verdict.get("ai"):
        slim["ai"] = {k: verdict["ai"].get(k) for k in ("available", "ok", "coherence", "quality", "summary",
                                                         "tokens", "retryable", "reason", "too_large", "quota",
                                                         "cap", "skipped", "model", "shrunk") if k in verdict["ai"]}
    data.setdefault("entries", []).append(slim)
    data["entries"] = [_scrub_entry(e) for e in data["entries"][-keep:]]
    with open(ledger_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)


def recent_heads(ledger_path, platform, n=30):
    """Headlines (title / first caption line) of the last n APPROVED videos in the ledger."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            entries = json.load(fh).get("entries", [])
    except (OSError, ValueError):
        return []
    return [e["head"] for e in entries if e.get("ok") and e.get("platform") == platform and e.get("head")][-n:]


def ai_reviews_today(ledger_path, now=None):
    """Real AI reviews (ai.available True) logged in this ledger on the current UTC day."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            entries = json.load(fh).get("entries", [])
    except (OSError, ValueError):
        return 0
    day = (now or datetime.now(timezone.utc)).date().isoformat()
    return sum(1 for e in entries
               if (e.get("checked_at") or "")[:10] == day and (e.get("ai") or {}).get("available") is True)


# Failures that mean the machinery is broken (missing tool, unset secret), not the video.
INFRA_RE = re.compile(r"could not run|not set|missing or empty|no such file|unavailable", re.I)


def failure_key(failures):
    """A stable key for an infrastructure failure, "" for content failures. Content
    failures (loudness, freeze, caption, AI blocks) vary from seed to seed and must
    never stop a retry loop; AI outages have their own handling in each bot."""
    if not failures:
        return ""
    first = str(failures[0])
    if not INFRA_RE.search(first) or first.startswith("AI review"):
        return ""
    return re.sub(r"\d", "#", first.lower())[:60]


def systemic(keys, n=3):
    """True when the last n candidates failed with the same infrastructure problem."""
    last = list(keys)[-n:]
    return len(last) == n and bool(last[0]) and len(set(last)) == 1 and not last[0].startswith("ai review")


def blocked_since(ledger_path, since_iso):
    """Blocked entries checked at or after since_iso (for the morning digest)."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            entries = json.load(fh).get("entries", [])
    except (OSError, ValueError):
        return []
    return [e for e in entries if not e.get("ok") and (e.get("checked_at") or "") >= since_iso]


def summary(verdict):
    """One line for Telegram: what was checked."""
    r = verdict.get("report", {})
    bits = [f"{r.get('res', '?')}", f"{r.get('seconds', '?')}s"]
    if r.get("lufs") is not None:
        bits.append(f"{r['lufs']} LUFS")
    if r.get("speech_match") is not None:
        bits.append(f"speech {r['speech_match']:.0%}")
    ai = verdict.get("ai") or {}
    if ai.get("available"):
        bits.append(f"AI {ai.get('coherence')}/10 coherent, {ai.get('quality')}/10 quality")
    return "🛡️ Upload gate passed: " + " · ".join(bits)


def block_message(verdict, what):
    """Telegram (HTML) text for a blocked video. Escapes everything."""
    import html
    lines = "\n".join(f"• {html.escape(f[:300])}" for f in verdict["failures"][:8])
    return (f"🛑 <b>Upload gate blocked: {html.escape(what)}</b>\n"
            f"{PLATFORMS[verdict['platform']]['name']} · {html.escape(verdict.get('file') or '?')}\n\n"
            f"{lines}\n\nNot posted.")
