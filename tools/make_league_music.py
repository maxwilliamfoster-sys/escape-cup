"""Cut the club league's music beds (assets/league_music) from licensed tracks.

History (Max, 2026-11-26): chord-tune pad -> original anthem ("nah") -> suspense music
"from Epidemic Sound" (paid subscription, breaks the £0 rule) -> free Kevin MacLeod
suspense tracks (CC BY 4.0, commit 11985a5) -> Max picked his own track from NCS
(NoCopyrightSounds): "Recall" by Gabriawll, https://youtu.be/LfAd6mGd81M.

NCS usage policy (ncs.io/usage-policy, checked 2026-11-26): free for independent creators
on TikTok/YouTube/etc., sport videos and monetising allowed; short-form needs no credit
(we credit anyway); NOT allowed: the music as the main focus, or "changing it beyond
cutting it to fit". So NCS beds are only cut, level-set and faded here (no EQ); the video
mix later low-passes everything together (Max's no-sharp-sounds rule) and ducks it under
the bounces, which is ordinary mixing, not a remix. The audio is NCS's own download
(ncs.io/api/download/<slug>), never ripped from YouTube.

Each track gives up to N 40 s beds (its loudest, steadiest stretches). The musical key of
every bed is measured and saved in credits.json, so keeper_audio plays the bounce notes in
the music's key; a bed with no clear key is dropped (the notes could clash).

Usage:  py tools/make_league_music.py <folder holding each TRACKS[i]["file"]>
"""
import json
import os
import subprocess
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render  # noqa: E402  (ffmpeg_bin)
from make_music_beds import BED_S, windows  # noqa: E402

OUT = os.path.join(ROOT, "assets", "league_music")
PROOF = os.path.join(OUT, "licences")
SR = 22050
MIN_KEY_CONF = 0.65        # below this the bed wanders between keys: bounce notes could clash, so drop it

NCS_POLICY = "https://ncs.io/usage-policy"
TRACKS = [
    {"file": "Recall.mp3", "slug": "ncs_gabriawll_recall", "beds": 3, "bpm": 127.0,   # bpm: ncs.io/recall
     "title": "Recall", "artist": "Gabriawll", "label": "NCS",
     "licence": "NCS usage policy (free for creators)",
     "credit": "Music: Gabriawll - Recall | Music provided by NoCopyrightSounds (NCS)",
     "source": "https://ncs.io/recall", "download": "https://ncs.io/api/download/recall",
     "youtube": "https://youtu.be/LfAd6mGd81M",
     "proof": {"policy": NCS_POLICY, "checked": "2026-11-26",
               "policy_said": ["Free for independent creators on YouTube, Twitch, Facebook, Instagram, "
                               "Snapchat, TikTok and other sites for user-made content.",
                               "Yes, you can monetise videos that use NCS music.",
                               "TikTok, Shorts and Reels need none [credit], because the sound names the track.",
                               "Not allowed: uploading the music on its own, releasing or selling it, or "
                               "changing it beyond cutting it to fit."],
               "video_description_said": "All NCS music is copyright free and safe for you to use on platforms "
                                         "like YouTube, TikTok and Twitch. You may not use any track for the "
                                         "purpose of creating a listening experience where the music is the "
                                         "primary focus of the video."}},
]

# Krumhansl-Kessler key profiles (C first)
MAJ = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MIN = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def key_of(path, ff):
    """(tonic pitch class, 'major'|'minor', confidence) from a chroma profile."""
    raw = subprocess.run([ff, "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64)
    size, hop = 8192, 4096
    f = np.fft.rfftfreq(size, 1 / SR)
    ok = (f > 55) & (f < 2000)
    pcs = (np.round(12 * np.log2(f[ok] / 440.0)) + 9).astype(int) % 12
    win = np.hanning(size)
    chroma = np.zeros(12)
    for s in range(0, len(x) - size, hop):
        mag = np.abs(np.fft.rfft(x[s:s + size] * win))[ok]
        chroma += np.bincount(pcs, weights=mag, minlength=12)
    best = []
    for k in range(12):
        for mode, prof in (("major", MAJ), ("minor", MIN)):
            best.append((float(np.corrcoef(np.roll(prof, k), chroma)[0, 1]), k, mode))
    best.sort(reverse=True)
    return best[0][1], best[0][2], round(best[0][0], 3)


def _decode(path, ff):
    raw = subprocess.run([ff, "-v", "error", "-i", path, "-ac", "1", "-ar", "44100", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def beat_steady(x, start, bpm, tol=0.015):
    """True if the beat phase agrees across four 10 s windows of x[start : start+40 s].
    (keeper_sync puts the bounces on this beat; a bed whose start has no clear kick would
    give the wrong grid: "Recall" from 10 s did, its first 12 s measured 0.398 vs 0.163.)"""
    import keeper_sync
    p = 60.0 / bpm
    ph = []
    for w in range(4):
        a = int((start + 10 * w) * 44100)
        seg = x[a:a + 10 * 44100]
        if len(seg) < 8 * 44100:
            return False
        ph.append((10 * w + keeper_sync.beat_grid(seg, 44100, bpm=bpm)[1]) % p)
    ref = ph[0]
    spread = max(abs((q - ref + p / 2) % p - p / 2) for q in ph)
    return spread <= tol


def main(src):
    ff = render.ffmpeg_bin()
    os.makedirs(PROOF, exist_ok=True)
    for d in (OUT, PROOF):                         # replace the previous beds
        for f in os.listdir(d):
            if f.endswith((".mp3", ".json")):
                os.remove(os.path.join(d, f))
    credits = {}
    for t in TRACKS:
        path = os.path.join(src, t["file"])
        whole = _decode(path, ff)
        track_s = len(whole) / 44100
        for tag, start in zip("abcd", windows(path, ff, t["beds"])):
            if t.get("bpm"):                       # slide later until the beat is steady
                for shift in range(0, 24, 2):
                    if start + shift + BED_S <= track_s and beat_steady(whole, start + shift, t["bpm"]):
                        start += shift
                        break
                else:
                    print(f"{t['slug']}_{tag}  dropped: no steady beat near {start} s")
                    continue
            name = f"{t['slug']}_{tag}.mp3"
            out = os.path.join(OUT, name)
            # cut + level + fades only: no EQ (NCS: no changes beyond cutting it to fit)
            subprocess.run([ff, "-v", "error", "-y", "-ss", str(start), "-t", str(BED_S), "-i", path,
                            "-af", "loudnorm=I=-20:TP=-2:LRA=11,afade=t=in:d=0.05,afade=t=out:st=39:d=1",
                            "-ar", "44100", "-b:a", "192k", out], check=True)
            pc, mode, conf = key_of(out, ff)
            if conf < MIN_KEY_CONF:
                os.remove(out)
                print(f"{name}  dropped: no clear key (r={conf})")
                continue
            credits[name] = {k: t[k] for k in ("title", "artist", "label", "licence", "credit", "source")}
            credits[name].update(from_s=start, key_pc=pc, mode=mode, key_conf=conf, bpm=t.get("bpm"))
            print(f"{name}  from {start} s  key {NAMES[pc]} {mode} (r={conf})")
        with open(os.path.join(PROOF, f"{t['slug']}.json"), "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in t.items() if k != "file"}, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT, "credits.json"), "w", encoding="utf-8") as fh:
        json.dump(credits, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(OUT, "LICENSES.txt"), "w", encoding="utf-8") as fh:
        fh.write("Music for the club league (league_music.py), made by tools/make_league_music.py.\n"
                 "Licence evidence at the time we checked: licences/<track>.json.\n"
                 "Each video's caption credits its track (league_music.credit).\n"
                 "Changes made: a 40 s section, level-set and faded; in the video it is mixed under the\n"
                 "sound effects (ducked, low-passed with the whole mix).\n\n")
        for name, c in credits.items():
            fh.write(f"{name}  (from {c['from_s']} s)\n  {c['credit']}\n  {c['licence']}: {c['source']}\n\n")


def download(dest):
    """Fetch every track from its label's official download link (never YouTube). The beds are
    NOT committed: the repo is public and NCS forbids "uploading the music on its own", so
    the posting/sample workflows run `--download` and cut fresh beds every run (2026-11-26)."""
    import urllib.request
    os.makedirs(dest, exist_ok=True)
    for t in TRACKS:
        req = urllib.request.Request(t["download"], headers={"User-Agent": "Mozilla/5.0 (escape-cup bot)"})
        with urllib.request.urlopen(req, timeout=60) as r, open(os.path.join(dest, t["file"]), "wb") as fh:
            data = r.read()
            if len(data) < 500_000 or not (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
                raise RuntimeError(f"{t['download']} did not return an MP3 ({len(data)} bytes)")
            fh.write(data)
        print(f"downloaded {t['file']} ({len(data) // 1024} KB) from {t['download']}")
    return dest


if __name__ == "__main__":
    if sys.argv[1] == "--download":
        import tempfile
        main(download(sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="league_music_")))
    else:
        main(sys.argv[1])
