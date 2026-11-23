# Gate Change (AirplaneMode app): notes for Claude

An airport-themed colour-and-number shedding card game, multiplayer over the hotspot with
bots. FastAPI in `app.py`, one page in `static/index.html`.

Part of AirplaneMode: the platform repo is `../airplanemode`. Read its `CLAUDE.md` for the
environment notes and `docs/app-contract.md` for the rules.

## Layout

- `engine.py`: the rules. Pure (no I/O, no clock, randomness only from the `rng` passed in).
  Cards are strings (`"sun.7"`, `"tide.skip"`, `"star.rev"`, `"peak.d2"`, `"wild"`, `"w4"`).
  `Game.act(seat, move)` either applies a move or raises `IllegalMove` and changes nothing.
  `Game.view(seat)` is the only thing that goes to a player.
- `bots.py`: easy/normal/hard. Pure functions of the game and an `rng`; they may only use
  public information (own hand, discards, hand sizes, scores, `game.voids`).
- `rooms.py`: rooms, seat tokens, long-poll (`Room.wait`), and one driver task per started
  room (bot moves, bot catches, autopilot takeover, Next round when no person is here).
  At `MAX_ROOMS` only lobbies/finished games are evicted; a live game never is (`RoomsFull`
  -> 503).
- `app.py`: HTTP. Token in `X-Player`; POSTs need `X-GateChange: 1`.

## Commands

```powershell
docker compose up -d --build                    # standalone dev: http://localhost:8771
.venv\Scripts\python tests\test_gatechange.py   # CI runs these before building
```

The venv (`py -3.10 -m venv .venv`, then `pip install -r requirements.txt httpx==0.28.1`)
is git-ignored.

## Rules

- **Original game, not a clone's branding.** Own name, airlines, card names and look. Never use
  the name of the well-known commercial game this resembles, its logo, its card design or its
  red/yellow/green/blue look. `test_page_keeps_its_own_identity` checks two things in
  `static/index.html`: that name never appears as a whole word (case-insensitive word-boundary
  match, so "unordered" or "unofficial" are fine), and the four airline colours (`--sun`,
  `--tide`, `--star`, `--peak`, as 6-digit hex) don't land one each in red, yellow, green and
  blue hue bands. Logos and card design aren't tested: check those by eye.
- **All rules on the server.** The page sends intents; `engine.py` decides.
- **Never leak hidden information**: other hands, the draw pile, whether a +4 was a bluff
  (`pending["guilty"]`), another player's drawn card. Tests check `view()`.
- Every suit has a glyph as well as a colour (colour-blind safety). Keep it that way.
- New moves: add to `Game.act`, `can` in `Game.view`, `GAME_MOVES` in `rooms.py`, the bots,
  and tests (including the bot-game loop that asserts 108 cards after every move).
- Page: relative URLs only (a test checks), no external assets, respect reduced motion.
- In-memory only: no volume. Memory limit 64m (measured ~31 MiB with 10 live tables).

## Untested

Not tried on real phones or the R6S yet: touch feel of the hand fan, long-poll behaviour through
Caddy on the box, phones locking mid-game (autopilot should cover it).

## Releasing

Push a `vX.Y.Z` tag. CI tests, then publishes `ghcr.io/jessewolcott/airplanemode-gatechange:X.Y.Z`
(arm64 + amd64). **Ask Jesse before pushing a release tag.** Released: 0.1.0, 0.1.1 (rejoin reclaims, unattended rounds advance, live games survive a full server), 0.1.2 (home button) (pinned in the platform's `compose.yaml`).
