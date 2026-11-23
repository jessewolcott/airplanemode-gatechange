# Architecture

## The pieces

```
 phone browser                           AirplaneMode box (Docker)
 ─────────────                           ─────────────────────────
 /gatechange/ (index.html) ──Caddy──>  gatechange:8000  (uvicorn, one process, 64m)
   │ GET  api/rooms                      app.py  ── HTTP, headers, status codes
   │ POST api/rooms | {code}/join          │
   │ GET  api/rooms/{code}/state?since=N   ├─ amrooms.Rooms registry: code -> GateRoom
   │      (long-poll, <= 20 s)             │    GateRoom(amrooms.Room): seats, tokens,
   │ POST api/rooms/{code}/act             │      options, version, Event, lock, driver
   │                                       ├─ engine.Game   one per started room (pure rules)
   └─ X-Player: <seat token>               └─ bots.decide   called by the driver
      X-GateChange: 1 on POSTs
```

- **`engine.py`** knows nothing about rooms, HTTP or time. `Game.act(seat, move)` applies a
  move or raises `IllegalMove` with a player-facing sentence and changes nothing.
  `Game.view(seat)` is the per-seat projection that goes over the wire.
- **`bots.py`** returns a list of moves for a seat (a `call` may come before a `play`).
- **`rooms.py`** owns everything stateful and time-based, on top of **`amrooms`** (the
  platform's Rooms SDK, `../airplanemode/sdk/rooms`, decision 0022). The SDK gives seats,
  tokens, host, join/leave/rejoin, the room version + `asyncio.Event` for long-poll, a
  per-room driver task and the registry. `GateRoom` adds the lobby options, moves, the view,
  and the driver's `plan()`: bot moves, bot catches, autopilot takeovers and (when no person
  is here) Next round.
- **`app.py`** maps HTTP to `Rooms`/`GateRoom` calls, maps the SDK's `RoomError` to its status
  and `IllegalMove` to 409, starts `rooms.sweep_forever()` in its lifespan, and serves the
  page and icon.
- The only other AirplaneMode service called is the admin's who's who lookup: `whois()`
  asks `ADMIN_URL` (default `http://admin:8000`) for `/api/me` before create/join, passing on
  only `X-Forwarded-For` and the `am_who` cookie. It's a label (seat name and emoji), never
  authorisation, and the game works without it (None on any failure). No Drop, no admin
  session, no model (`app.yaml`: `needs: {llm: none}`).

## Request flow: a move

1. The player taps a card. The page (`tapCard`) checks it's their turn and the index is in
   `game.playable`; for a wild it opens the airline picker first.
2. `POST api/rooms/{code}/act {type: "play", card, suit?}` with `X-Player` and
   `X-GateChange: 1`.
3. `app.act` takes `room.lock`, calls `Room.act(token, data)`: seat lookup by token
   (constant-time compare), `_clean_move` validates card/suit/target shapes, `Game.act` applies.
4. `Room.changed()` bumps `version`, records `changed_at`, re-rolls the next bot delay, sets
   and replaces the `asyncio.Event` (waking every long-poll and the driver), and re-plans
   bot catches for a missed Last call.
5. The response is the caller's own `Room.view(token)`; every other phone's pending
   `state?since=` returns with its own view.
6. The driver wakes, plans the next due item (bot move, catch or takeover) and sleeps until then.

## Process model

Single uvicorn worker (`CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]`),
running as an unprivileged user (uid 10001). All rooms live in one `amrooms.Rooms` instance
(`rooms = make_rooms()`) in `app.py`. The per-room `asyncio.Lock` only serialises within the process, and long-poll
wake-ups are `asyncio.Event`s, so **more than one worker would split rooms between
processes** (a code created on one worker is a 404 on another). Don't add `--workers`.

Everything is plain asyncio in one thread; there are no `await`s inside `Room.act`/`view`, so
reads in `state` (not under the lock) never see a half-applied move.

FastAPI's docs/OpenAPI endpoints are disabled (`app.py:42`).

## How it plugs into the platform

- Platform `compose.yaml` service `gatechange`: image
  `ghcr.io/jessewolcott/airplanemode-gatechange:0.1.2` (pinned), `mem_limit
  ${GATECHANGE_MEM:-64m}`, `restart: unless-stopped`, no env, no volume.
  `hardware/nanopi-r6s.env` sets `GATECHANGE_MEM=64m`, `hardware/glinet.env` `48m`.
- Platform `compose.dev.yaml` builds this repo as `airplanemode-gatechange:dev`.
- Platform `Caddyfile`: `redir /gatechange /gatechange/` and
  `handle_path /gatechange/* { reverse_proxy gatechange:8000 }`. `handle_path` strips the
  prefix, so the app sees `/`, `/api/rooms`, ... That's why the page uses relative URLs.
  Long-poll needs no Caddy config (plain HTTP request held up to 20 s).
- Platform `portal/apps.json` entry `gatechange` (category Games, `uses_llm: false`, icon
  `/icons/gatechange.svg`). Platform CI checks image tag, Caddy route and apps.json entry.
- `app.yaml` (this repo): id, port 8000, path `/gatechange/`, memory 64m, no volume,
  `needs: {llm: none}`, documented env vars.

## Configuration (environment, read at import in `rooms.py:32-35` and `:265-266`)

| Var | Default | Meaning | Documented in |
|---|---|---|---|
| `GATECHANGE_MAX_ROOMS` | `200` | Rooms kept at once; creating one more evicts the least recently touched lobby/finished room, never a game in progress (503 if all are) | README, app.yaml |
| `GATECHANGE_ROOM_IDLE` | `7200` | Seconds untouched before a room is swept | README, app.yaml |
| `GATECHANGE_BOT_DELAY` | `1.0` | Base bot "thinking" delay (s); tests use 0 | README, app.yaml |
| `GATECHANGE_AWAY` | `45` | Seconds without contact before a seat shows `away` (autopilot takes it on their turn) | README, app.yaml |
| `GATECHANGE_IDLE_TURN` | `60` | Seconds someone may sit on their turn | README, app.yaml |
| `GATECHANGE_POLL_WAIT` | `20` | Long-poll hold time (s); tests use 0.3 | README, app.yaml |
| `GATECHANGE_SEED` | unset | Fixed seed: room `rng` seeded `"{SEED}-{code}"` | README, app.yaml |
| `ADMIN_URL` | `http://admin:8000` | Read by `amrooms.whois` on each call; empty turns who's who off (tests do) | app.yaml |

`GATECHANGE_AWAY`, `_POLL_WAIT`, `_BOT_DELAY` and `_IDLE_TURN` are `GateRoom` class
attributes; `_MAX_ROOMS`, `_ROOM_IDLE` and `_SEED` go to `Rooms` in `make_rooms()`. All are
read once at import. Tests that change them at runtime assign the class attributes
(`rooms.GateRoom.idle_turn = 0.2`) or the registry's (`gc.rooms.max_rooms`, `gc.rooms.idle`)
rather than env vars.

## Fixed constants

| Name | Where | Value |
|---|---|---|
| `HAND_SIZE` | engine | 7 |
| `MAX_EVENTS` | engine | 80 (event log kept per game) |
| `DEFAULT_RULES` | engine | `{"stack": False, "draw_until": False, "target": 500}` |
| `CODE_ALPHABET` | amrooms | `ABCDEFGHJKMNPQRSTUVWXYZ` (no I, L, O) |
| `GateRoom.max_seats` | rooms | 8 |
| `GateRoom.auto_next_round` | rooms | 8.0 s |
| `empty_grace` | amrooms `Rooms` | 60 s (a game everyone left is kept this long after its last change) |
| `SOLO_BOTS` | rooms | 3 (normal) |
| `GateRoom.bot_names` | rooms | Ada, Kofi, Mira, Jun, Lou, Ines, Teo, Noor, Ravi, Sam |
| `FORGET_CALL` | bots | easy 0.3, normal 0.05, hard 0.0 |
| `CATCH_CHANCE` | bots | easy 0.25, normal 0.6, hard 0.9 |
| open-games list | amrooms `open_lobbies` | at most 30, most recently touched first |
