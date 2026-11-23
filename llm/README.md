# Gate Change: how it's built (LLM / engineer reference)

Checked-in notes, written from the code (release 0.1.0 plus the fixes after it, 2026-11-22).
They're kept out of the container image by `.dockerignore` (`llm/`; its `*.md` pattern only
matches files at the build-context root). If the code and these notes disagree, the code wins;
fix the notes.
Nothing here has been checked on real phones or on the R6S.

## Overview

Gate Change is an AirplaneMode app: an airport-themed colour-and-number shedding card game
(2-8 seats, people and bots) for every phone on the travel router's hotspot, with no
internet. It is an original game in a familiar genre: four "airlines" are the suits (Sunward,
Tidewater, Starline, Summit, each with a colour *and* a glyph), with Delayed (skip), U-Turn
(reverse), Extra Baggage +2, Standby (wild) and Overbooked +4 (challengeable wild), plus a
"Last call!" rule.

The backend is FastAPI in four small modules: `engine.py` (pure rules, ~440 lines),
`bots.py` (pure bot decisions, ~140), `rooms.py` (rooms, seat tokens, long-poll, one asyncio
"driver" task per started room, ~440) and `app.py` (HTTP, ~130). A table is a **room** with a
4-letter code. Each person gets a secret seat token kept in their browser; every request
identifies the seat by it (`X-Player`). Clients **long-poll** `state?since=<version>` (returns
as soon as the room version moves, or after 20 s). The server enforces every rule and sends
each seat only its own view (`Game.view(seat)`): never other hands, the draw pile, or whether
a +4 was a bluff. Bots play on the server with a human-feeling delay; a bot also plays the
seat of anyone who leaves, goes quiet (45 s) or sits on their turn (60 s), until they take it
back. Everything is in memory: no volume, no files, 64 MB limit. The whole UI is one file,
`static/index.html` (~960 lines of HTML/CSS/vanilla JS, inline SVG glyphs, no external assets).

## Files in this folder

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | The pieces, request flow, process model, platform wiring, config |
| [engine-and-rules.md](engine-and-rules.md) | `engine.py`: cards, deck, turn phases, every move, Last call, scoring, `view()` |
| [rooms-and-long-poll.md](rooms-and-long-poll.md) | `rooms.py`: rooms, seats, tokens, lobby actions, long-poll, the driver, autopilot, expiry |
| [bots.md](bots.md) | `bots.py`: easy/normal/hard decisions, catching, public-information rule |
| [api.md](api.md) | `app.py`: every endpoint, headers, status codes, response shapes |
| [table-page.md](table-page.md) | `static/index.html`: screens, polling loop, rendering, hand, actions, results, a11y |
| [testing-and-release.md](testing-and-release.md) | Tests, dev setups, Docker image, CI, releasing, platform wiring |
| [gotchas-and-extending.md](gotchas-and-extending.md) | Non-obvious behaviour, quirks, and how to add things safely |

## Source map

| Path | Role |
|---|---|
| `engine.py` | The rules (pure: no I/O, no clock, randomness only from the passed `rng`) |
| `bots.py` | Bot decisions (pure functions of game + `rng`) |
| `rooms.py` | Rooms, seats, long-poll, bot/autopilot driver, room registry (`Rooms`) |
| `app.py` | HTTP layer (FastAPI); module docstring lists the API |
| `static/index.html` | The whole frontend; `static/icon.svg` the icon |
| `tests/test_gatechange.py` | Engine, bot, room and API tests (plain script, no pytest) |
| `Dockerfile`, `requirements.txt`, `.dockerignore` | Image (python:3.12-slim, fastapi 0.115.6, uvicorn 0.34.0) |
| `compose.yaml` | Standalone dev on `http://localhost:8771` |
| `app.yaml` | AirplaneMode app manifest |
| `.github/workflows/image.yml` | CI: tests, then multi-arch image to GHCR |
| `CLAUDE.md`, `README.md` | Rules for Claude and the user-facing description |
| `docs/icon.svg`, `docs/screenshots/` | README images (laptop browser at phone size) |
| `../airplanemode/docs/decisions/0016-games-and-couch.md` | The design decision (shared with Passport, Wayfarer, The Couch) |

## The rules that must not break (from `CLAUDE.md`)

1. **Original game, not a clone's branding.** Own name, airlines, card names and look. Never
   the commercial game's name, logo, card design or red/yellow/green/blue look. (The test
   only greps the page for the name; the colours are a review rule.)
2. **All rules on the server.** The page sends intents; `engine.py` decides.
3. **Never leak hidden information**: other hands, the draw pile, `pending["guilty"]`,
   another player's drawn card. Tests check `view()`.
4. Every suit has a glyph as well as a colour (colour-blind safety).
5. New moves: `Game.act`, `can` in `Game.view`, `GAME_MOVES` in `rooms.py`, the bots, and
   tests (including the bot-game loop that asserts 108 cards after every move).
6. Page: relative URLs only (a test checks), no external assets, respect reduced motion.
7. In-memory only: no volume. Memory limit 64m (CLAUDE.md: measured ~31 MiB with 10 live tables).
8. Ask Jesse before pushing a `v*` tag (it publishes the image).
