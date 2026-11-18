"""Upbeat house music under the marble sounds.

Max, 2026-11-18: "add non distracting background music to make the video more interesting
but it is important that the marble sound is still loud enough". Same day: calm piano didn't
fit the vibe, Kevin MacLeod suspense was "a bit better", then: a different source, upbeat house.
An earlier music bed (v2, 2026-09-29) was rejected, so this one stays under the marbles and
dull-edged:
- 40 s beds cut from CC BY house tracks on SoundCloud (tools/make_music_beds.py,
  assets/music/LICENSES.txt, proof in assets/music/licences/); credit(seed) is the caption
  line the licence needs;
- low-passed (audio.MUSIC_LOWPASS) so hi-hats can never sound sharp (house rule, and
  quality.py's 2 kHz gate);
- mixed MUSIC_DB below the marble track, dipped under every ring break, lifted in the
  last-ring phase, and dropped away at the win (audio.build does the mixing).

bed() never raises: if a clip can't be decoded the video is made without music and the
run log says so, rather than losing a posting slot.
"""
import json
import os
import random
import subprocess

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(ROOT, "assets", "music")
SR = 44100
FADE_IN = 0.15          # s; suspense music is already driving on frame 0 (just no click)
ENABLED = True


def clips():
    if not os.path.isdir(MUSIC_DIR):
        return []
    return sorted(os.path.join(MUSIC_DIR, f) for f in os.listdir(MUSIC_DIR) if f.endswith(".mp3"))


def _duration(path, ffmpeg):
    """Clip length in seconds from ffmpeg's banner (no ffprobe needed)."""
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            h, m, s = line.split(",")[0].split()[1].split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 40.0                                    # every bed was cut to 40 s


def pick(seed):
    """The clip a seed uses (None if there are none). Pure, so the caption can credit it before rendering."""
    files = clips()
    if not ENABLED or not files:
        return None
    return random.Random(f"music-{seed}").choice(files)


def credit(seed):
    """CC BY 4.0 credit line for the seed's clip ("" if no music)."""
    path = pick(seed)
    if not path:
        return ""
    try:
        with open(os.path.join(MUSIC_DIR, "credits.json"), encoding="utf-8") as fh:
            c = json.load(fh)[os.path.basename(path)]
    except (OSError, KeyError, ValueError):
        return ""
    # No URL: the upload gate rejects links in TikTok captions (they aren't clickable there anyway);
    # the full source link is in assets/music/LICENSES.txt.
    return f"Music: \"{c['title']}\" by {c['artist']} on SoundCloud, {c['licence']}"


def bed(seed, seconds, ffmpeg):
    """Mono float32 array of `seconds` of music at SR, RMS-normalised to 1.0, or None."""
    path = pick(seed)
    if not path:
        print("[music] no music clips found; video made without music")
        return None
    rng = random.Random(f"music-offset-{seed}")
    try:
        dur = _duration(path, ffmpeg)
        start = rng.uniform(0.0, max(0.0, dur - seconds - 0.5))
        raw = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{start:.2f}", "-t", f"{seconds + 0.2:.2f}",
                              "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                             capture_output=True, check=True, timeout=60).stdout
    except Exception as e:  # noqa: BLE001 - music is a nice-to-have, a slot is not
        print(f"[music] could not decode {os.path.basename(path)} ({type(e).__name__}: {e}); no music")
        return None
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    n = int(seconds * SR)
    if len(x) < SR:
        print(f"[music] {os.path.basename(path)} decoded to {len(x)} samples; no music")
        return None
    if len(x) < n:                                  # a clip shorter than the video: fade out early
        tail = min(len(x), int(1.0 * SR))
        x[-tail:] *= np.linspace(1.0, 0.0, tail)
        x = np.concatenate([x, np.zeros(n - len(x))])
    x = x[:n]
    nf = min(n, int(FADE_IN * SR))
    x[:nf] *= np.linspace(0.0, 1.0, nf) ** 2
    rms = np.sqrt(np.mean(x ** 2)) or 1.0
    print(f"[music] {os.path.basename(path)} from {start:.1f} s")
    return x / rms
