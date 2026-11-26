"""Music for the club league (Beat The Keeper), 2026-11-26.

Max's pick: "Recall" by Gabriawll from NCS (NoCopyrightSounds), free for creators under the
NCS usage policy (see tools/make_league_music.py for the terms and why the beds are only cut).
Beds live in assets/league_music, each with its measured key in credits.json so keeper_audio
plays the bounce notes in the music's key. Same picking/decoding as the cup's music.py.
"""
import json
import os
import random

import music

ROOT = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(ROOT, "assets", "league_music")
ENABLED = True


def _credits():
    try:
        with open(os.path.join(MUSIC_DIR, "credits.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def clips():
    if not os.path.isdir(MUSIC_DIR):
        return []
    known = _credits()
    return sorted(os.path.join(MUSIC_DIR, f) for f in os.listdir(MUSIC_DIR)
                  if f.endswith(".mp3") and f in known)


def pick(seed):
    """The clip a seed uses (None if none). Pure, so the caption can credit it before rendering."""
    files = clips()
    if not ENABLED or not files:
        return None
    return random.Random(f"league-music-{seed}").choice(files)


def info(seed):
    path = pick(seed)
    return _credits().get(os.path.basename(path)) if path else None


def credit(seed):
    """The track's credit line for the caption ("" if no music). No URL: captions reject links."""
    c = info(seed)
    if not c:
        return ""
    return c["credit"]


def key(seed):
    """Tonic pitch class of the seed's clip as a MAJOR key (a minor key -> its relative major,
    which has the same pentatonic notes), or None."""
    c = info(seed)
    if not c:
        return None
    return (c["key_pc"] + 3) % 12 if c["mode"] == "minor" else c["key_pc"]


def bed(seed, seconds, ffmpeg):
    path = pick(seed)
    return music.bed(seed, seconds, ffmpeg, path=path) if path else None
