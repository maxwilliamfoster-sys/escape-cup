"""
Upload gate: the last check before any video leaves the machine (growth-lab
MASTER, vendored into each bot like growth_stats / experiment_loop. Edit here,
re-copy, run each repo's tests).

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

GATE_VERSION = "2026-10-16"

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
    "ai_context": "",                # one paragraph: what the channel posts and why
    "ai_realistic": False,           # realistic AI voice/visuals -> platform AI label required
    "allowed_domains": (),           # links allowed in descriptions (YouTube only)
    "ledger": None,                  # path of the JSON audit log (None = no log)
}

AI_MODEL = os.environ.get("UPLOAD_GATE_AI_MODEL", "qwen/qwen3.8-27b")
AI_URL = "https://api.groq.com/openai/v1/chat/completions"

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
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+|\b[a-z0-9-]+\.(?:com|net|org|io|co|uk|ly|gg|tv|me|app|xyz)/\S*",
                     re.I)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
PHONE_RE = re.compile(r"(?:\+44\s?7\d{3}|\b07\d{3})\s?\d{3}\s?\d{3}\b|\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
HASHTAG_RE = re.compile(r"(?<![\w#])#\w+")

POLICY_BRIEF = {
    "tiktok": (
        "TikTok Community Guidelines + For You feed eligibility: no gore, blood, dead bodies, "
        "injuries or distressing/shocking imagery; no sexual content; nothing that sexualises or "
        "endangers minors; no hate or slurs; no dangerous acts; no unverified claims stated as fact; "
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


# ----------------------------------------------------------------- layer 1+2 --
def check_text(platform, *, title="", caption="", description="", tags=(), recent_texts=(),
               allowed_domains=(), ai_realistic=False, ai_label=None):
    """Platform limits + policy rules for the words that ship with the video.
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
    spam = sorted(set(low_tags) & SPAM_TAGS)
    if spam:
        warn.append(f"generic reach hashtags {' '.join(spam)} (no reach benefit; can read as spam)")
    if platform == "tiktok" and len(hashtags) > 5:
        warn.append(f"{len(hashtags)} hashtags - TikTok search uses the first few")

    # Repetition: the same headline as a recent post is the "repetitive content" signal.
    head = _norm_space(title if platform == "youtube_shorts" else caption.split("\n")[0])
    recent = {_norm_space(t) for t in recent_texts if t}
    if head and head in recent:
        fail.append(f"same {'title' if platform == 'youtube_shorts' else 'first caption line'} "
                    f"as a recent post (repetitive content): \"{head[:80]}\"")

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


def _images_b64(path, dur, sheets=2, cols=3, rows=2, tile=(216, 384)):
    """[first frame at 360x640, then `sheets` contact sheets of cols*rows frames spread
    evenly through the video, left-to-right, top-to-bottom]. The model takes <=3 images."""
    import tempfile
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        first = os.path.join(tmp, "first.jpg")
        _run([ffmpeg_bin(), "-loglevel", "error", "-ss", "0.3", "-i", path, "-frames:v", "1",
              "-vf", "scale=360:640", "-q:v", "5", first], 120)
        n = sheets * cols * rows
        _run([ffmpeg_bin(), "-loglevel", "error", "-i", path, "-vf",
              f"fps={n / max(dur, 1):.5f},scale={tile[0]}:{tile[1]},tile={cols}x{rows}", "-frames:v", str(sheets),
              "-q:v", "5", os.path.join(tmp, "sheet%d.jpg")], 300)
        for name in ["first.jpg"] + [f"sheet{i}.jpg" for i in range(1, sheets + 1)]:
            p = os.path.join(tmp, name)
            if not os.path.exists(p) or not os.path.getsize(p):
                raise RuntimeError(f"could not grab frames ({name})")
            with open(p, "rb") as fh:
                out.append(base64.b64encode(fh.read()).decode())
    return out


def ai_review(path, platform, profile, *, title="", caption="", transcript="", seconds=None):
    """Ask a free vision model to watch the video as a strict pre-publication reviewer.
    Returns {"ok": bool, "available": bool, "coherence", "quality", "problems": [...], "summary"}."""
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return {"ok": False, "available": False, "problems": ["GROQ_API_KEY not set"]}
    dur = seconds or float(probe(path)["format"]["duration"])
    images = _images_b64(path, dur)
    content = [{"type": "text", "text": (
        f"You are the final pre-publication reviewer for a {PLATFORMS[platform]['name']} channel. "
        # Without the date the model called 2025/2026 events "future dates" and blocked them.
        f"Today's date is {datetime.now(timezone.utc):%d %B %Y}; events up to today are in the past.\n"
        f"Channel: {profile.get('ai_context') or 'short vertical videos'}\n"
        f"Image 1 is the first frame (what a scroller sees). Images 2-3 are contact sheets of 12 "
        f"frames spread evenly through the {dur:.0f}s video, in reading order.\n"
        f"Title: {title or '-'}\nCaption: {caption or '-'}\n"
        f"Narration script (verified to match the audio): {(transcript or '(no narration)')[:2500]}\n\n"
        f"Platform rules: {POLICY_BRIEF[platform]}\n\n"
        "Judge: (1) COHERENCE - do the visuals, on-screen text, narration and caption tell one "
        "consistent story that matches the title, with nothing irrelevant, contradictory, garbled, "
        "cut off or glitched? (2) QUALITY - would a viewer see this as a finished, watchable video "
        "(readable text, no broken rendering, not a placeholder)? (3) POLICY - any rule above broken?\n"
        "Ground rules: a plain request to comment, like or follow is ALLOWED (only rewards or "
        "false promises are bait). Burned-in captions show one or a few words at a time, so a "
        "lone word in one frame is normal. Do not fact-check the story: facts were verified "
        "against sources before this step; judge only what is on screen and how it fits. Stock "
        "or mood footage is fine when its mood fits the story; flag footage that is jarringly "
        "unrelated or inappropriate for the subject (e.g. food, animals, parties, sport, "
        "celebrations in a story about a death).\n"
        "Only BLOCK for a concrete problem you can point to in the frames or text, not for style.\n"
        'Reply with JSON only: {"verdict":"PASS" or "BLOCK","coherence":1-10,"quality":1-10,'
        '"policy_issues":["..."],"quality_issues":["..."],"summary":"one sentence"}')}]
    for b64 in images:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}})
    # Reasoning off: hidden reasoning was ~3k tokens a review, and the model's free
    # quota is 200k tokens/DAY shared by all three bots (calibration day 2026-10-16
    # exhausted it). The verdict is a visual judgement; it doesn't need a chain.
    body = {"model": AI_MODEL, "temperature": 0, "max_tokens": 900, "reasoning_effort": "none",
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}]}
    last, res, tokens = None, {}, None
    for attempt in range(3):
        try:
            req = urllib.request.Request(AI_URL, data=json.dumps(body).encode(), headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "upload-gate"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                reply = json.load(resp)
            out = reply["choices"][0]["message"]["content"]
            tokens = (reply.get("usage") or {}).get("total_tokens")
            m = re.search(r"\{.*\}", out, re.S)
            res = json.loads(m.group(0)) if m else {}
            if res.get("verdict") not in ("PASS", "BLOCK"):
                raise ValueError(f"no verdict in reply: {out[:200]}")
            break
        except urllib.error.HTTPError as e:
            body_txt = e.read()[:400].decode(errors="replace")
            last = f"HTTP {e.code}: {body_txt[:200]}"
            if "per day" in body_txt:              # daily quota gone: waiting a minute won't help
                return {"ok": False, "available": False, "quota": True,
                        "problems": ["AI review skipped: the free daily AI quota is used up "
                                     "(it refills over the next 24h)"]}
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(min(65, float(e.headers.get("retry-after") or 30)))
                continue
            return {"ok": False, "available": False, "problems": [f"AI review failed: {last}"]}
        except Exception as e:                     # network / JSON trouble
            last = str(e)[:200]
            if attempt < 2:
                time.sleep(10)
                continue
            return {"ok": False, "available": False, "problems": [f"AI review failed: {last}"]}
    coh, qual = int(res.get("coherence") or 0), int(res.get("quality") or 0)
    problems = [str(p) for p in (res.get("policy_issues") or []) + (res.get("quality_issues") or []) if p]
    ok = res["verdict"] == "PASS" and coh >= 5 and qual >= 5 and not (res.get("policy_issues") or [])
    return {"ok": ok, "available": True, "coherence": coh, "quality": qual, "problems": problems[:6],
            "summary": str(res.get("summary") or "")[:300], "model": AI_MODEL, "tokens": tokens}


# ---------------------------------------------------------------------- main --
def _head(platform, title, caption):
    return (title if platform == "youtube_shorts" else (caption or "").split("\n")[0]).strip()


def check(path, platform, *, profile=None, title="", caption="", description="", tags=(), script="",
          recent_texts=(), ai_label=None, extra_failures=()):
    """Run every layer. Returns the verdict dict; approves the file for require() only when ok."""
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform}")
    pr = {**DEFAULT_PROFILE, **(profile or {})}
    if pr.get("ledger"):                         # headlines this gate approved before count as recent
        recent_texts = list(recent_texts) + recent_heads(pr["ledger"], platform)
    verdict = {"ok": False, "platform": platform, "file": os.path.basename(path), "sha256": None,
               "head": _head(platform, title, caption),
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
        ai_realistic=pr["ai_realistic"], ai_label=ai_label))
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
    if mode != "off" and not F:                  # don't spend the AI budget on a video already blocked
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


def record(verdict, ledger_path, keep=300):
    """Append to the JSON audit log (what was checked, when, and why it passed or failed)."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {"entries": []}
    slim = {k: verdict.get(k) for k in ("checked_at", "platform", "file", "sha256", "head", "ok", "failures",
                                        "warnings", "gate_version")}
    slim["report"] = {k: verdict["report"].get(k) for k in ("seconds", "lufs", "speech_match", "longest_freeze")}
    if verdict.get("ai"):
        slim["ai"] = {k: verdict["ai"].get(k) for k in ("available", "ok", "coherence", "quality", "summary",
                                                         "tokens")}
    data.setdefault("entries", []).append(slim)
    data["entries"] = data["entries"][-keep:]
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
