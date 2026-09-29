# Escape Cup

A physics tournament for TikTok. 32 nations, single elimination, one match per video.
Two flag-balls start inside six rotating rings; a ring shatters when a ball breaks
through its gap, and whoever breaks the final ring wins. No music: the collisions are the soundtrack
(soft taps and plinks scaled by impact speed, panned to where they happen).

- `sim.py` – deterministic physics (same seed ⇒ same match). Nothing is scripted.
- `director.py` – picks which seed to publish (length, no dead stretches, lead changes). Never picks the winner.
- `render.py` – pycairo frames piped to FFmpeg, 1080×1920 @ 60fps. `audio.py` – numpy-synthesised ASMR-style collision sounds.
- `tournament.py` – the bracket. `tournament_state.json` holds an ordered log of every scheduled match; the bracket is always rebuilt by replaying it, so winners advance exactly as posted and a new cup (fresh draw) starts after each Final. Each run reconciles with Buffer: if a scheduled video is deleted (your veto) or rejected, that match and every later unpublished one are pulled and replayed in order.
- `quality.py` – the gate every video must pass before it's scheduled: technical specs, black/frozen frames, hook text, winner card, loudness, no sharp highs, excluded flags, caption rules (≤5 hashtags, keywords, no engagement bait, no spoilers, no repeats).
- `publish.py` – hosts the MP4 on the `media` branch (GitHub Pages) and schedules it on TikTok through the Buffer API.

## Running

    python main.py --sample --local   # render the next match to output/, no network
    python main.py --sample           # render + send to Telegram, no post
    python main.py                    # render + gate + schedule in Buffer (CI does this daily)
    python main.py --reset-unpublished  # cancel our pending Buffer posts; redraw if nothing published

Posting plan: 1/day at 19:30 UK for the first 7 days, then weekdays 16:30 + 20:00, weekends 12:00 + 19:30.

Secrets: `BUFFER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Flags from flagcdn.com (public domain). Fonts: Montserrat (OFL).

