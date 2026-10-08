"""Download the 32 flags once (run locally; the PNGs are committed)."""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from teams import ALL_TEAMS as TEAMS  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "flags")
os.makedirs(OUT, exist_ok=True)
for code, name, _ in TEAMS:
    dest = os.path.join(OUT, f"{code}.png")
    if os.path.exists(dest):
        continue
    req = urllib.request.Request(f"https://flagcdn.com/w640/{code}.png",
                                 headers={"User-Agent": "escape-cup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())
    print("fetched", code, name)
