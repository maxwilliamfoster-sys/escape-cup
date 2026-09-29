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

## Music

Background tracks are CC0 submissions from OpenGameArt, fetched by `tools/fetch_music.py`
(which rejects any page not licensed CC0 only). Bounce notes are synthesised in each track's
detected key. No credit is required; listed here anyway:

- [Beats n games](https://opengameart.org/content/beats-n-games) by da9elb (CC0)
- [Bouncy Hamster Dancing (Menu Music?)](https://opengameart.org/content/bouncy-hamster-dancing-menu-music) by cynicmusic (CC0)
- [Catchy](https://opengameart.org/content/catchy) by Spring Spring (CC0)
- [City Loop](https://opengameart.org/content/city-loop-0) by wipics (CC0)
- [Electronic Synth](https://opengameart.org/content/electronic-synth) by Pro Sensory (CC0)
- [Elevator Music](https://opengameart.org/content/elevator-music) by Pro Sensory (CC0)
- [Free Fall](https://opengameart.org/content/free-fall) by TAD (CC0)
- [Fruity](https://opengameart.org/content/fruity) by Pro Sensory (CC0)
- [Game Music #1 "Life is a Melody"](https://opengameart.org/content/game-music-1-life-is-a-melody) by djyan (CC0)
- [Get Ready](https://opengameart.org/content/get-ready) by Loyalty Freak Music (CC0)
- [Greens are good for you!](https://opengameart.org/content/greens-are-good-for-you) by Pro Sensory (CC0)
- [Hypnotic Chill (Extended 4 minute mix)](https://opengameart.org/content/hypnotic-chill-extended-4-minute-mix) by cynicmusic (CC0)
- [Menu Music](https://opengameart.org/content/menu-music-1) by wipics (CC0)
- [Montage](https://opengameart.org/content/montage) by wipics (CC0)
- [One Step at a time](https://opengameart.org/content/one-step-at-a-time) by Pro Sensory (CC0)
- [Reggae](https://opengameart.org/content/reggae) by Pro Sensory (CC0)
- [Richer](https://opengameart.org/content/richer) by Pro Sensory (CC0)
- [Space Synth Wave](https://opengameart.org/content/space-synth-wave) by Pro Sensory (CC0)
- [Talking Cute](https://opengameart.org/content/talking-cute) by Pro Sensory (CC0)

