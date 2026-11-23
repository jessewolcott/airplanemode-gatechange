# Testing, dev setups, build, CI and release

## Tests (`tests/test_gatechange.py`)

A plain script, not pytest: `python tests/test_gatechange.py` (or
`.venv\Scripts\python tests\test_gatechange.py`). Needs `requirements.txt` plus
`httpx==0.28.1` for FastAPI's `TestClient`. Venv: `py -3.10 -m venv .venv`, then
`pip install -r requirements.txt httpx==0.28.1` (git-ignored).

How it works:
- Sets `GATECHANGE_BOT_DELAY=0` and `GATECHANGE_POLL_WAIT=0.3` **before** importing, so
  bots move instantly and long-polls return fast.
- `__main__` runs every `test_*` in definition order inside one `TestClient(gc.app)` (the
  context manager gives the app a running event loop, which the driver tasks need), prints
  ok/FAIL per test, exits 1 on any failure. API tests share the one `gc.rooms` registry.
- Helpers: `rig(g, hands, top, suit, current, pile)` puts a game in a known position
  (unused cards become the pile); `illegal(g, seat, move)` asserts `IllegalMove` **and**
  that the game's `__dict__` is unchanged; `play_out` runs a bot match asserting 108 cards
  after every move; `my_move(view)` picks a simple legal move from a person's view.

| Test | Checks |
|---|---|
| `test_deck` | 108 cards, 25 per airline, one 0, two of each other rank, 4+4 wilds, points, 54 distinct |
| `test_deal` | 7 each, number top card, 108 total, seat 0 starts; 1 or 9 players rejected |
| `test_matching_and_illegal_moves` | playable set, refusals (no match, not held, not your turn, wild without/with bad airline, unknown move, non-dict, bad seat, pass/accept/challenge out of phase), number match changes suit, after a wild only the named suit |
| `test_action_cards` | skip, reverse (4 players), +2, U-Turn with 2 players acts as skip |
| `test_overbooked_challenge` | bluff caught (+4 to the player, challenger plays on), honest +4 (+6, skipped), accept; `guilty` never in the view |
| `test_drawing` | drawn-playable phase, only the drawn card, keep it, unplayable draw passes, voids |
| `test_draw_until_rule` | keeps drawing until playable |
| `test_reshuffle_and_blocked_round` | reshuffle keeps the top card; blocked round: lowest hand wins |
| `test_last_call` | exposure + catch by anyone, call after playing is safe, calling ahead, window closes on the next move, too early with 3 cards |
| `test_stacking_rule` | +2 on +2 accumulates, only d2 answers, accept takes the total |
| `test_scoring_and_rounds` | points, round_over, revealed hands, next round rotates first player, game over at 500, single round |
| `test_view_hides_secrets` | per-seat hand, no `draw_pile`/`hands`/`guilty` in any view, spectator view |
| `test_bot_games_always_finish` | 400 bot matches (see bots.md) |
| `test_bot_games_are_repeatable` | same seed, same result |
| `test_hard_bot_is_better` | hard > easy over 300 heads-up rounds |
| `test_bot_choices` | specific bot decisions |
| `test_page_and_health` | `/health`; page has "Gate Change" and `api/rooms` relative and no `"/api/`; no absolute/root-relative `src`/`href`; icon is SVG |
| `test_page_keeps_its_own_identity` | the commercial game's name never appears as a whole word (`\buno\b`, case-insensitive); the four `--sun/--tide/--star/--peak` hex colours don't fall one each into red/yellow/green/blue hue bands (`hue_family`) |
| `test_header_and_tokens_required` | 403 without header/token, 404 unknown room, 400 non-object body |
| `test_lobby_and_multiplayer_game` | code alphabet, open-games listing, join (lowercase code, duplicate name suffix, HTML kept as text), host-only lobby actions, options, private rooms unlisted, late join 409, per-seat hands, illegal move leaves `version` unchanged, two people + a bot play a whole single round, rematch host-only, back to lobby |
| `test_long_poll` | same version waits >= 0.25 s; old version returns at once |
| `test_solo_game_vs_bots` | solo room starts with 3 bots, unlisted; human plays to 500 (accepts 409 when a bot moved first) |
| `test_rejoin_leave_and_host_handover` | rejoin with token keeps the seat; host leaves -> guest is host; last person leaves -> 404 |
| `test_autopilot_and_reclaim` | with `rooms.IDLE_TURN = 0.2` the idle seat goes auto, a bot plays it, `reclaim` restores; rejoining with the token also clears `auto` |
| `test_bots_catch_a_missed_last_call` | with hard catch chance 1.0, a forgotten call is caught by a bot (+2) |
| `test_next_round_when_nobody_is_here` | at `round_over` the table waits while the person is present; once they're away (`AWAY_AFTER = 0.5`) the driver starts the next round; with the person on autopilot a whole match to 500 plays out unattended |
| `test_full_server_never_closes_a_live_game` | `MAX_ROOMS = 3`: lobbies are evicted first, games in progress never; all live -> 503 with a clear detail; a `game_over` room can be evicted (clears the registry first) |
| `test_rooms_are_capped_and_expire` | `MAX_ROOMS = 5` caps the registry; `ROOM_IDLE = -1` sweeps everything |

Not covered by tests: the page's JavaScript (rendering, polling loop, hand layout, toasts),
long-poll through Caddy, real network latency. Check those by hand in a browser at phone
size, ideally on real phones.

## Dev setups

1. **Standalone** (this repo's `compose.yaml`, project `gatechange-dev`):
   `docker compose up -d --build` -> `http://localhost:8771` (`PORT`), `mem_limit: 64m`, no
   other services needed. VS Code tasks: "Standalone: up/down", "Tests: Gate Change"
   (default test task).
2. **Platform dev stack**: from `../airplanemode`,
   `docker compose -f compose.yaml -f compose.dev.yaml up -d --build` builds this repo as
   `airplanemode-gatechange:dev`; open `http://localhost:8080/gatechange/`. This exercises the
   Caddy prefix and the relative URLs, as on the box.
   Multiplayer in dev: open several browser profiles/windows (each needs its own
   `localStorage`, since the token lives there; a private window works).

Port 8000 is reserved by Windows on Jesse's machine; that's why the standalone port is 8771.

## Image

`Dockerfile`: `python:3.12-slim`, `pip install -r requirements.txt` (fastapi 0.115.6, uvicorn
0.34.0), `COPY . .`, user `gatechange` (uid 10001), `EXPOSE 8000`, single uvicorn process.
`.dockerignore` drops `.git`, `.github`, `.vscode`, `.venv`, `tests`, `__pycache__`,
top-level `*.md`, `compose.yaml`, `docs`, `llm/`. No volume, no healthcheck in the image
(`/health` exists for callers).

## CI (`.github/workflows/image.yml`)

On push to `main`, `v*` tags and PRs: Python 3.12, install requirements + httpx, run the
tests, then QEMU + buildx build for `linux/arm64,linux/amd64`. Pushes to
`ghcr.io/<owner>/airplanemode-gatechange` except on PRs: branch tag (`main`) and semver
`X.Y.Z` from `vX.Y.Z` tags.

## Releasing

1. Tests pass locally; check the page in the platform dev stack; retake
   `docs/screenshots/` if the UI changed.
2. **Ask Jesse before pushing a `vX.Y.Z` tag** (it publishes the image).
3. Bump the pinned tag in the platform `compose.yaml` (never `latest`); platform CI checks
   tag, Caddy route and `portal/apps.json` entry. Update the platform CLAUDE.md status.

Released so far: 0.1.0 (pinned on the platform).

## Untested (from CLAUDE.md / README)

Not tried on real phones or the R6S: touch feel of the hand fan, long-poll through Caddy on
the box, phones locking mid-game, memory under load on the device (CLAUDE.md quotes ~31 MiB
with 10 live tables, measured in dev), the GL.iNet 48m limit.
