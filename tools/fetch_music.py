"""
Build the background-music library from OpenGameArt CC0 submissions.

For each candidate page: accept it only if its ONLY licence is CC0, download the
first audio file, convert to a loudness-normalised 44.1 kHz MP3 (max 150 s),
detect its key, and record title/author/source/key in assets/music/tracks.json.
Run locally; the MP3s are committed so CI never downloads anything.

    python tools/fetch_music.py            # fetch any missing candidates
    python tools/fetch_music.py --rekey    # recompute keys only
"""
import html
import json
import os
import re
import subprocess
import sys
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from render import ffmpeg_bin  # noqa: E402

MUSIC = os.path.join(ROOT, "assets", "music")
INDEX = os.path.join(MUSIC, "tracks.json")
CANDIDATES = [
    "bouncy-hamster-dancing-menu-music", "game-music-1-life-is-a-melody", "catchy",
    "tropical-loop", "city-loop-0", "beats-n-games", "short-plingy-loop", "space-synth-wave",
    "free-fall", "montage", "fruity", "good-morning", "up-in-the-sky",
    "hypnotic-chill-extended-4-minute-mix", "greens-are-good-for-you", "talking-cute",
    "one-step-at-a-time", "heavenly-loop", "get-ready", "oldskool", "menu-music-1",
    "electronic-synth", "reggae", "a-simple-trifle", "elevator-music", "richer",
]
UA = {"User-Agent": "escape-cup-music-fetch/1.0"}

MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def inspect(slug):
    page = get(f"https://opengameart.org/content/{slug}").decode("utf-8", "replace")
    block = re.search(r"field-name-field-art-licenses(.*?)field-name-collect", page, re.S)
    lics = re.findall(r"class='license-name'>([^<]+)<", block.group(1)) if block else []
    title = html.unescape(re.search(r"<title>(.*?)\s*\|", page).group(1)).strip()
    author = re.search(r"Author:&nbsp;.*?<a href=\"/users/[^\"]+\"[^>]*>([^<]+)</a>", page, re.S)
    files = re.findall(r'href="(https://opengameart\.org/sites/default/files/[^"]+\.(?:mp3|ogg|wav|flac))"',
                       page)
    return {"slug": slug, "title": title, "licenses": lics,
            "author": html.unescape(author.group(1)) if author else "unknown", "files": files}


def decode(path, sr=22050):
    raw = subprocess.run([ffmpeg_bin(), "-loglevel", "error", "-i", path, "-ac", "1", "-ar", str(sr),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32), sr


def detect_key(path):
    """Krumhansl-Schmuckler on a whole-track chromagram. Returns (tonic_pc, mode, confidence)."""
    x, sr = decode(path)
    n, hop = 8192, 4096
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    band = (freqs > 60) & (freqs < 4000)
    pcs = (np.round(12 * np.log2(freqs[band] / 440.0)) + 9) % 12
    chroma = np.zeros(12)
    for i in range(0, len(x) - n, hop):
        mag = np.abs(np.fft.rfft(x[i:i + n] * win))[band]
        np.add.at(chroma, pcs.astype(int), mag)
    chroma /= chroma.sum() or 1
    best = (-2, 0, "major")
    for tonic in range(12):
        for mode, prof in (("major", MAJOR), ("minor", MINOR)):
            r = np.corrcoef(chroma, np.roll(prof, tonic))[0, 1]
            if r > best[0]:
                best = (r, tonic, mode)
    return best[1], best[2], round(float(best[0]), 3)


def main():
    os.makedirs(MUSIC, exist_ok=True)
    index = json.load(open(INDEX)) if os.path.exists(INDEX) else []
    have = {t["slug"] for t in index}
    if "--rekey" in sys.argv:
        for t in index:
            t["tonic"], t["mode"], t["key_conf"] = detect_key(os.path.join(MUSIC, t["file"]))
    for slug in CANDIDATES:
        if slug in have:
            continue
        try:
            info = inspect(slug)
        except Exception as e:
            print(f"skip {slug}: {e}")
            continue
        if info["licenses"] != ["CC0"] or not info["files"]:
            print(f"skip {slug}: licences={info['licenses']} files={len(info['files'])}")
            continue
        src_url = info["files"][0]
        tmp = os.path.join(MUSIC, "_dl" + os.path.splitext(src_url)[1])
        with open(tmp, "wb") as f:
            f.write(get(src_url))
        out = f"{slug}.mp3"
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", tmp, "-t", "150",
                        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "2",
                        "-b:a", "160k", os.path.join(MUSIC, out)], check=True)
        os.remove(tmp)
        dur = len(decode(os.path.join(MUSIC, out))[0]) / 22050
        if dur < 35:
            print(f"skip {slug}: only {dur:.0f}s")
            os.remove(os.path.join(MUSIC, out))
            continue
        tonic, mode, conf = detect_key(os.path.join(MUSIC, out))
        index.append({"slug": slug, "file": out, "title": info["title"], "author": info["author"],
                      "source": f"https://opengameart.org/content/{slug}", "license": "CC0",
                      "seconds": round(dur, 1), "tonic": tonic, "mode": mode, "key_conf": conf})
        print(f"ok   {slug}: {info['title']} by {info['author']} {dur:.0f}s key {NAMES[tonic]} {mode} ({conf})")
    json.dump(index, open(INDEX, "w"), indent=1)
    print(f"{len(index)} tracks")


if __name__ == "__main__":
    main()
