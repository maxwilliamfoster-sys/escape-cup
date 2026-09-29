# Escape Cup

A physics tournament for TikTok. 32 nations, single elimination, one match per video.
Two flag-balls start inside six rotating rings; a ring shatters when a ball breaks
through its gap, and whoever breaks the final ring wins. Every bounce plays the
next note of a public-domain melody.

- `sim.py` – deterministic physics (same seed ⇒ same match). Nothing is scripted.
- `director.py` – picks which seed to publish (length, no dead stretches, lead changes). Never picks the winner.
- `render.py` – pycairo frames piped to FFmpeg, 1080×1920 @ 60fps. `audio.py` – numpy-synthesised soundtrack.
- `tournament.py` – bracket state in `tournament_state.json` (committed by CI).
- `publish.py` – hosts the MP4 on the `media` branch (GitHub Pages) and schedules it on TikTok through the Buffer API.

## Running

    python main.py --sample --local   # render the next match to output/, no network
    python main.py --sample           # render + send to Telegram, no post
    python main.py                    # render + schedule in Buffer (CI does this daily)

Secrets: `BUFFER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Flags from flagcdn.com (public domain). Fonts: Montserrat (OFL).
