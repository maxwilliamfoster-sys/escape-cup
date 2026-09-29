"""
Quality gate: every video must pass ALL of these before it is hosted or
scheduled. main.py re-renders with a different match on failure and never
posts a video that failed.

Why each check exists (research 2026-09-29, TikTok Community Guidelines
effective 2026-09-24 + creator data):
  technical  1080x1920 H.264/AAC 60fps, healthy bitrate - TikTok re-encodes
             everything; a weak source comes out blurry and gets swiped.
  visual     no black frames, no frozen stretches, hook text on frame 0,
             winner card at the end - the first second decides the swipe.
  audio      loudness in a normal band and almost no energy above 2 kHz (the
             owner found brighter versions painful), no dead silence.
  content    no flags carrying scripture (see teams.py), both teams scored,
             a real winner.
  caption    <= 5 hashtags (TikTok only counts 5), keywords present, no
             engagement bait (TikTok FYF-ineligible: "like-for-like", false
             incentives for following, misleading claims), no spoiler,
             not a repeat of a recent caption (automation that "sends
             repetitive content" is listed as spam).
"""
import json
import os
import re
import subprocess

import numpy as np

import teams
from render import ffmpeg_bin

W, H, FPS = 1080, 1920, 60
EXCLUDED_CODES = {"sa", "iq", "af", "ir"}          # flags carrying scripture
LUFS_RANGE = (-19.0, -13.0)
MAX_TRUE_PEAK = -0.5
MAX_HF_PCT = 1.0                                   # % of energy above 2 kHz
MIN_VIDEO_KBPS = 1500
MAX_BYTES = 90 * 1024 * 1024                       # GitHub Pages file limit is 100 MB
DURATION_RANGE = (22.0, 48.0)
BAIT = [r"\bfollow\b", r"\blike\s*(for|4)\s*like\b", r"\bl4l\b", r"\bf4f\b", r"\blike if\b",
        r"\bgift", r"\bshare (this|if)\b", r"\bsubscribe\b", r"#fyp", r"#foryou", r"#viral",
        r"\b\d+%\s*(of people|can't|cannot|can not|fail)", r"\bnobody can\b", r"\bonly \d+%"]
SPOILERS = [r"\bwins\b", r"\bwinner\b", r"\bbeats?\b", r"\bknocked out\b", r"\beliminat"]
KEYWORDS = ["escape", "simulation", "country", "physics"]


def _ffprobe():
    ff = ffmpeg_bin()
    probe = ff.replace("ffmpeg.exe", "ffprobe.exe") if ff.endswith(".exe") else ff[:-len("ffmpeg")] + "ffprobe"
    return probe if os.path.exists(probe) else "ffprobe"


def _run(args):
    return subprocess.run(args, capture_output=True, text=True, errors="replace")


def probe(path):
    r = _run([_ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    return json.loads(r.stdout)


def _filter_log(path, vf=None, af=None, until=None):
    args = [ffmpeg_bin(), "-hide_banner", "-nostats", "-i", path]
    if until:
        args += ["-t", f"{until:.2f}"]
    if vf:
        args += ["-vf", vf, "-an"]
    if af:
        args += ["-af", af, "-vn"]
    args += ["-f", "null", "-"]
    return _run(args).stderr


def _frame(path, t):
    raw = subprocess.run([ffmpeg_bin(), "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(H, W, 3).astype(np.int16)


def _audio_bands(path):
    raw = subprocess.run([ffmpeg_bin(), "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "44100",
                          "-f", "f32le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.float32).astype(np.float64)
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / 44100)
    tot = spec.sum() or 1.0
    return 100 * spec[f > 2000].sum() / tot, 100 * spec[(f > 1000) & (f <= 2000)].sum() / tot


def check_video(path, match_seconds):
    """Returns (report, failures). match_seconds = length before the winner card."""
    rep, fail = {}, []
    info = probe(path)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v or not a:
        return rep, ["missing video or audio stream"]
    num, den = map(int, v["avg_frame_rate"].split("/"))
    fps = num / den if den else 0
    dur = float(info["format"]["duration"])
    size = int(info["format"]["size"])
    vkbps = int(v.get("bit_rate") or 0) / 1000 or (size * 8 / dur / 1000)
    rep.update(res=f"{v['width']}x{v['height']}", fps=round(fps, 2), codec=f"{v['codec_name']}/{a['codec_name']}",
               seconds=round(dur, 1), mbps=round(vkbps / 1000, 1), mb=round(size / 1e6, 1))
    if (v["width"], v["height"]) != (W, H):
        fail.append(f"resolution {v['width']}x{v['height']}")
    if abs(fps - FPS) > 0.5:
        fail.append(f"fps {fps:.2f}")
    if v["codec_name"] != "h264" or v.get("pix_fmt") != "yuv420p" or a["codec_name"] != "aac":
        fail.append(f"codec {rep['codec']} {v.get('pix_fmt')}")
    if not DURATION_RANGE[0] <= dur <= DURATION_RANGE[1]:
        fail.append(f"duration {dur:.1f}s outside {DURATION_RANGE}")
    if vkbps < MIN_VIDEO_KBPS:
        fail.append(f"video bitrate {vkbps:.0f} kbps < {MIN_VIDEO_KBPS}")
    if size > MAX_BYTES:
        fail.append(f"file {size / 1e6:.0f} MB too big")

    # visual: black frames anywhere, frozen picture during the match
    blk = re.findall(r"black_duration:([\d.]+)", _filter_log(path, vf="blackdetect=d=0.1:pix_th=0.03"))
    if blk:
        fail.append(f"black frames ({', '.join(blk)}s)")
    frz = re.findall(r"freeze_duration: ([\d.]+)",
                     _filter_log(path, vf="freezedetect=n=0.0005:d=0.8", until=match_seconds))
    if frz:
        fail.append(f"frozen picture during match ({', '.join(frz)}s)")
    rep["black_frames"], rep["freezes"] = len(blk), len(frz)

    # first frame must carry the yellow hook text in the header band
    f0 = _frame(path, 0.0)
    band = f0[140:225, 60:1020]
    yellow = int(((band[..., 0] > 200) & (band[..., 1] > 170) & (band[..., 2] < 140)).sum())
    rep["hook_px"] = yellow
    if yellow < 800:
        fail.append("hook text not visible on the first frame")
    # the winner card must actually appear (screen dims under the card)
    mid = _frame(path, match_seconds * 0.5).mean()
    card = _frame(path, min(dur - 0.3, match_seconds + 2.0))
    if card[1150:1300, :, :].max() < 200 or card.mean() > mid * 0.9:
        fail.append("winner card not detected at the end")

    # audio
    log = _filter_log(path, af="ebur128=peak=true")
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", log)
    lufs = float(lufs[-1]) if lufs else -99.0
    tp = float(peak[-1]) if peak else 0.0
    hf, mid_band = _audio_bands(path)
    rep.update(lufs=lufs, true_peak=tp, hf_pct=round(hf, 2), band_1_2k_pct=round(mid_band, 1))
    if not LUFS_RANGE[0] <= lufs <= LUFS_RANGE[1]:
        fail.append(f"loudness {lufs} LUFS outside {LUFS_RANGE}")
    if tp > MAX_TRUE_PEAK:
        fail.append(f"true peak {tp} dBTP")
    if hf > MAX_HF_PCT:
        fail.append(f"{hf:.2f}% energy above 2 kHz (too sharp)")
    sil = re.findall(r"silence_duration: ([\d.]+)",
                     _filter_log(path, af="silencedetect=n=-55dB:d=3", until=match_seconds))
    if sil:
        fail.append(f"dead silence during match ({', '.join(sil)}s)")
    return rep, fail


def check_content(a, b, winner, breaks):
    fail = []
    for code in (a, b):
        if code in EXCLUDED_CODES:
            fail.append(f"excluded flag {code}")
        if code not in teams.BY_CODE:
            fail.append(f"unknown team {code}")
    if winner not in (a, b):
        fail.append("winner is not one of the two teams")
    if min(breaks) < 1:
        fail.append("a team broke no rings (one-sided match)")
    return fail


def check_caption(caption, a, b, recent_captions):
    fail = []
    tags = re.findall(r"#\w+", caption)
    if not 3 <= len(tags) <= 5:
        fail.append(f"{len(tags)} hashtags (want 3-5; TikTok only counts 5)")
    if len(set(t.lower() for t in tags)) != len(tags):
        fail.append("duplicate hashtag")
    low = caption.lower()
    for pat in BAIT:
        if re.search(pat, low):
            fail.append(f"engagement-bait phrase /{pat}/")
    first = caption.split("\n")[0]
    for pat in SPOILERS:
        if re.search(pat, low):
            fail.append(f"possible spoiler /{pat}/")
    if teams.name(a).lower() not in first.lower() or teams.name(b).lower() not in first.lower():
        fail.append("first line must name both countries (search keywords)")
    if not any(k in low for k in KEYWORDS):
        fail.append("no niche keyword in caption")
    if len(first) > 110:
        fail.append(f"first line {len(first)} chars - gets cut off before 'more'")
    if len(caption) > 2000:
        fail.append("caption too long")
    body = caption.rsplit("\n", 1)[0].strip()
    if any(body == c.rsplit("\n", 1)[0].strip() for c in recent_captions):
        fail.append("caption repeats a recent one")
    return fail
