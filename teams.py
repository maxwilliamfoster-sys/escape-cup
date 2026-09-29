"""The 32 nations of the Escape Cup.

Chosen for big TikTok audiences and football rivalries (comment fuel).

Deliberately excluded:
  - India: TikTok is banned there, so an Indian fanbase can't find the videos.
  - Any flag carrying scripture (Saudi Arabia, Iraq, Afghanistan, Iran...): e.g. the
    Saudi flag bears the Shahada and must not touch the ground - FIFA holds it
    above the pitch. Putting it on a ball that bounces and loses invites offence
    and mass-reporting. quality.py refuses these codes.
`color` is the ball's trail/accent colour, picked by hand from each flag.
Flags are from flagcdn.com (national flags are public domain), fetched once by
tools/fetch_flags.py and committed so CI needs no network for them.
"""

TEAMS = [
    # code, name, trail colour
    ("br", "Brazil", "#1DB954"),
    ("ar", "Argentina", "#6CACE4"),
    ("fr", "France", "#2F5BEA"),
    ("gb-eng", "England", "#E8E8E8"),
    ("es", "Spain", "#F1BF00"),
    ("de", "Germany", "#FFCE00"),
    ("pt", "Portugal", "#E42518"),
    ("it", "Italy", "#1FA64A"),
    ("nl", "Netherlands", "#FF7F00"),
    ("be", "Belgium", "#FDDA24"),
    ("hr", "Croatia", "#E8112D"),
    ("ma", "Morocco", "#C1272D"),
    ("jp", "Japan", "#FF2A4F"),
    ("kr", "South Korea", "#3A6FD8"),
    ("us", "USA", "#4A7BF7"),
    ("mx", "Mexico", "#10A35B"),
    ("ca", "Canada", "#FF3030"),
    ("au", "Australia", "#FFCD00"),
    ("ng", "Nigeria", "#12B35C"),
    ("eg", "Egypt", "#E0303D"),
    ("sn", "Senegal", "#00B350"),
    ("tr", "Turkey", "#F0303A"),
    ("pl", "Poland", "#F03050"),
    ("ua", "Ukraine", "#FFD500"),
    ("ph", "Philippines", "#2E6BE6"),
    ("id", "Indonesia", "#FF3B3B"),
    ("vn", "Vietnam", "#FFD700"),
    ("ch", "Switzerland", "#FF3B30"),
    ("co", "Colombia", "#FCD116"),
    ("uy", "Uruguay", "#7FB6F0"),
    ("ie", "Ireland", "#22B35E"),
    ("gb-sct", "Scotland", "#3C8DFF"),
]

BY_CODE = {c: (c, n, col) for c, n, col in TEAMS}

# Home-nation flags are emoji tag sequences, not two regional indicators.
_SUBDIVISION_EMOJI = {
    "gb-eng": "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F",
    "gb-sct": "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
}


def name(code):
    return BY_CODE[code][1]


def color(code):
    return BY_CODE[code][2]


def emoji(code):
    if code in _SUBDIVISION_EMOJI:
        return _SUBDIVISION_EMOJI[code]
    return "".join(chr(0x1F1E6 + ord(ch) - ord("a")) for ch in code)


def hashtag(code):
    return "#" + name(code).replace(" ", "").lower()
