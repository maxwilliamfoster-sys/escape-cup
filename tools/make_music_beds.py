"""Cut the background-music beds in assets/music from a list of licensed tracks.

History (all 2026-11-18, Max): calm piano "doesn't match the vibe" -> high-tempo suspense
(Kevin MacLeod) "a bit better" -> wanted a different source, upbeat HOUSE. The house tracks
come from SoundCloud with the licence filter "to modify commercially" and are kept only if
the track's own licence field is exactly cc-by (CC BY 3.0 on SoundCloud), it is the
producer's own original (no remix / edit / bootleg / set), and no sample or lease wording.
Same rules as giftok-bot/cc_music.py. Proof (the SoundCloud metadata at the time we
checked) goes to assets/music/licences/<id>.json.

Each track gives up to two 40 s beds: its loudest, steadiest stretches (the drop, not the
intro). Mono, low-passed at 5.5 kHz, -20 LUFS; audio.py low-passes again and sets the level.

Usage:  py tools/make_music_beds.py <manifest.json> <folder with <id>.mp3 files>
manifest = [{"id", "title", "artist", "url", "license", "proof": {...}}, ...]
"""
import json
import os
import re
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import render  # noqa: E402  (ffmpeg_bin)

OUT = os.path.join(ROOT, "assets", "music")
PROOF = os.path.join(OUT, "licences")
BED_S = 40
SR = 22050
LICENCE_NAME = {"cc-by": "CC BY 3.0"}          # SoundCloud's cc-by links to the 3.0 deed


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40]


def windows(path, ff, n=2):
    raw = subprocess.run([ff, "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    db = 20 * np.log10(np.sqrt(np.mean(x[: len(x) // SR * SR].reshape(-1, SR) ** 2, axis=1)) + 1e-9)
    scored = sorted(((db[s:s + BED_S].mean() - 0.5 * db[s:s + BED_S].std(), s)
                     for s in range(0, max(1, len(db) - BED_S), 2)), reverse=True)
    picks = []
    for _, s in scored:
        if all(abs(s - p) >= BED_S for p in picks):
            picks.append(s)
        if len(picks) == n:
            break
    return picks


def main(manifest, src):
    ff = render.ffmpeg_bin()
    tracks = json.load(open(manifest, encoding="utf-8"))
    os.makedirs(PROOF, exist_ok=True)
    for f in os.listdir(OUT):                     # replace the previous beds
        if f.endswith(".mp3") or f in ("credits.json",):
            os.remove(os.path.join(OUT, f))
    for f in os.listdir(PROOF):
        os.remove(os.path.join(PROOF, f))
    credits = {}
    for t in tracks:
        if (t.get("license") or "").lower() not in LICENCE_NAME:
            raise SystemExit(f"{t['title']}: licence {t.get('license')} is not allowed")
        path = os.path.join(src, f"{t['id']}.mp3")
        for tag, start in zip("ab", windows(path, ff)):
            name = f"{slug(t['artist'])}_{slug(t['title'])}_{tag}.mp3"
            subprocess.run([ff, "-v", "error", "-y", "-ss", str(start), "-t", str(BED_S), "-i", path,
                            "-ac", "1", "-af", "lowpass=f=5500,loudnorm=I=-20:TP=-2:LRA=7,"
                            "afade=t=in:d=0.05,afade=t=out:st=39:d=1",
                            "-ar", "44100", "-b:a", "128k", os.path.join(OUT, name)], check=True)
            credits[name] = {"title": t["title"], "artist": t["artist"], "url": t["url"],
                             "licence": LICENCE_NAME[t["license"].lower()], "from_s": start, "id": t["id"]}
            print(f"{name}  (from {start} s)")
        with open(os.path.join(PROOF, f"{t['id']}.json"), "w", encoding="utf-8") as fh:
            json.dump(t.get("proof") or t, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT, "credits.json"), "w", encoding="utf-8") as fh:
        json.dump(credits, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT, "LICENSES.txt"), "w", encoding="utf-8") as fh:
        fh.write("Background music for Escape Cup (music.py), made by tools/make_music_beds.py.\n"
                 "Every track is licensed by its creator under Creative Commons Attribution 3.0\n"
                 "(https://creativecommons.org/licenses/by/3.0/), as shown on its SoundCloud page when checked;\n"
                 "the page data at that time is saved in licences/<id>.json.\n"
                 "Each video's caption credits its track (main.with_music_credit, music.credit).\n"
                 "Changes made: a 40 s section, mono, low-passed, loudness-normalised, mixed under sound effects.\n\n")
        for name, c in credits.items():
            fh.write(f"{name}  (from {c['from_s']} s)\n  \"{c['title']}\" by {c['artist']}, {c['licence']}\n  {c['url']}\n\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
