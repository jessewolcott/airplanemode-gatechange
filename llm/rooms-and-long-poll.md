# Rooms, seats, long-poll and the driver (`rooms.py` on `amrooms`)

Everything is in memory. Restarting the container loses every table (decision 0016 accepts
this).

`rooms.py` is the game on top of **`amrooms`**, the platform's Rooms SDK (platform decision
0022; source and README in [`../../airplanemode/sdk/rooms/`](../../airplanemode/sdk/rooms/README.md)).
The SDK owns seats, tokens, host, join/leave/rejoin, presence, long-poll, the driver loop and
the room registry. `rooms.py` adds the table options, starting games, the moves, the per-seat
view and what the driver does (bot moves, bot catches, autopilot, Next round). Read the SDK
README for its rules; this file says what Gate Change uses and changes.

| Line | What |
|---|---|
| `:28` | `class GateRoom(Room)` and its settings |
| `:59` | `start()` |
| `:71` | `act(token, data)`: every request from a person |
| `:136` | `view(token)` |
| `:159` | `_watch_exposure()`: bot catch timers |
| `:175`, `:179`, `:205` | `plan()`, `_plan()`, `_execute()`: the driver hook |
| `:222` | `_bot_move()` |
| `:243` | `_clean_move()` |
| `:263` | `make_rooms()`: the registry `app.py` uses |

## What comes from amrooms

- **`Seat`**: `kind` (`human`/`bot`), `name`, `avatar` (who's who emoji or None), `person`
  (who's who id or None), `level` (bots), `token` (`secrets.token_urlsafe(18)` for humans),
  `seen` (monotonic time of the last request), `left` (pressed Leave during a game), `auto`
  (the app put a bot on this person's seat).
- **Names**: `clean_name` collapses whitespace, cuts to 20 chars, drops non-printables,
  fallback `"Traveller"`. HTML is **not** stripped (`"Jesse <b>"` stays): the page escapes.
  Names are unique per room, case-insensitive (`"Sam 2"`), also on rename. Bots get a random
  unused name from `GateRoom.bot_names`, else `"Bot N"`.
- **`join(name, token, who)`**: your seat back if the token is yours or `who["id"]` matches a
  seat's `person` (another browser on the same device, no token needed). Either way it clears
  `left` and `auto`, refreshes `seen`, takes the who's who id and emoji, renames if a name (or
  a who's who name) is given, and makes you host if the room has none. **Works mid-game.**
  Otherwise a new seat, lobby only: `RoomError(409)` "That game has already started." /
  "That table is full (8 seats)."
- **`leave(token)`**: lobby: the seat goes. Game: `left = True`, the seat stays (a bot plays
  it, see `on_autopilot`). The host role passes to the next person who hasn't left (or None).
- **`seen(token)`**: refreshes the seat's `seen`, and the room's `touched` only for a seated
  person who hasn't left (spectators and a leaver's last poll keep nothing alive). Never undoes
  a leave or autopilot.
- **Lobby helpers** used by `act`: `add_bot`, `remove_seat` (people too; never the host),
  `set_bot_level`, `seat_arg`. Their refusals are `RoomError`: 400 for bad input ("Unknown
  difficulty.", "No such seat." (bools rejected), "Only bots have a difficulty."), 409 for
  "The table is full (8 seats).", "The host can't be removed.", "The game has started."
- **`changed()` / `wait(since)`**: version + `asyncio.Event` long-poll (below). `close()`
  marks the room closed, cancels the driver and wakes polls (they then get 404).
- **`seat_info`**, **`summary()`** (open-games list: `code, host` (name or "?"), `players,
  humans, max`), **`idle(i)`** (no request for `away_after`, whatever the flags say).
- **The driver** (`ensure_driver`): one task per room with a game. It asks `plan(now)` for
  `(when, action)`, runs `action()` under `room.lock` once due, then `changed()`. It re-plans
  on every change and at least every 2 s.
- **`Rooms`**: codes, eviction, sweeping, `open_lobbies()` (below).

## `GateRoom`

Settings are class attributes, read from the environment once at import (`env_float`):

| Attribute | Env | Default | Meaning |
|---|---|---|---|
| `max_seats` | - | 8 | |
| `bot_levels` | - | `bots.LEVELS` | easy / normal / hard |
| `away_after` | `GATECHANGE_AWAY` | 45 | s without a request: away |
| `poll_wait` | `GATECHANGE_POLL_WAIT` | 20 | s a long-poll waits |
| `bot_delay` | `GATECHANGE_BOT_DELAY` | 1.0 | base bot "thinking" delay; 0 in tests |
| `idle_turn` | `GATECHANGE_IDLE_TURN` | 60 | s someone may sit on their turn |
| `auto_next_round` | - | 8.0 | s at "Round over" before the driver moves on, if nobody's here |

Tests change them on the class (`rooms.GateRoom.idle_turn = 0.2`) and restore them.

Extra fields: `public`, `options` `{"mode": "500"|"single", "stack", "draw_until"}`,
`next_delay`, `catchers`, `seen_exposed`. Seats are ordered; index = engine seat.

- `playing()`: a game whose phase isn't `game_over` (so a finished game can be evicted and
  bots can be added again after `lobby`).
- `changed()`: the SDK's, plus `next_delay = bot_delay * uniform(0.75, 1.3)` and
  `_watch_exposure()`.
- `on_autopilot(s)`: a bot, or a person with `auto` or `left`.

### Lobby (`game is None`)

Host-only `act` types: `add_bot {level}`, `remove {seat}` (not the host), `set_level
{seat, level}` (bots only), `options {mode?, stack?, draw_until?, public?}`, `start`.
Non-host -> `IllegalMove` "Only the host can change the table." Any other type in the lobby
-> "The game hasn't started yet."

`start()`: needs >= 2 seats; builds `Game(len(seats), {stack, draw_until, target: 500 or 0},
Random(room.rng.random()))`; seats that had `left` start on autopilot; `ensure_driver()`.

### In a game

`act` types: `reclaim` (clear own `auto`/`left`), `next_round` (**any** seat, only in
`round_over`; the driver also does it when no person is here), `rematch` (host, only in
`game_over`; `start()` again with the same seats), `lobby` (host; drops the game, clears
`auto`, removes seats that left), and the engine moves in `GAME_MOVES = {play, draw, pass,
accept, challenge, call, catch}`. A game move first goes through `_clean_move` (unknown card
-> "Unknown card.", bad suit -> "Unknown airline.", non-int target -> "No such player."), then
`Game.act`; a successful move clears the person's `auto` and `left`. `act` ends with
`changed()` only if nothing raised (the SDK lobby helpers also call `changed()` themselves,
so a lobby change moves the version by 2). `leave` returns straight after `Room.leave`.

`act` raises `IllegalMove` (409) for game and lobby refusals, and the SDK's `RoomError` for
"You're not at this table." (403) and the lobby helpers above.

### `view(token)` (`:136`)

Calls `seen(token)`. Returns `code, version, state` (`lobby` or the engine `phase`), `you`
(seat index), `is_host, public, options, seats[{name, avatar, bot, level, host, left, auto,
away}]`, `game` (`Game.view(you)` or None). Here `away` is `idle()`: no request for
`away_after`, apart from the `auto`/`left` flags.

## Long-poll

`Room.wait(since)` (SDK): return at once if `since != version`; otherwise wait on the
current Event up to `poll_wait` (20 s). The `state` endpoint calls `view` before waiting
(marks you present) and again after, so an open page refreshes `seen` at least every ~20 s,
well inside `away_after`. `since=-1` (the default) always returns immediately.

## The driver hook (`plan` / `_plan` / `_execute`)

The SDK loop calls `plan(now)`; `GateRoom.plan` wraps `_plan`, which returns the earliest
due item `(when, priority, kind, seat)` or None (no game, room closed, `game_over`):

- In `round_over`: one item `(when, 3, "next_round", None)` with `when = max(changed_at +
  auto_next_round, seen + away_after for each person who isn't auto or left)`, so it only
  falls due once nobody is here; re-planning picks up fresh `seen` from polls.
- In `playing`:
  - `catch` (priority 0): one per bot in `catchers`;
  - `move` (priority 1) if the current seat is `on_autopilot`: `changed_at + next_delay`,
    plus `1.5 * bot_delay` when someone is exposed and a person (not auto, not left) other
    than the exposed seat could catch them;
  - `takeover` (priority 2) if the current seat is a person not on autopilot:
    `min(changed_at + idle_turn, seen + away_after)`.

When it's due, the SDK takes `room.lock` and calls the action, `_execute(now)`, which
re-plans and runs the item (catch -> `Game.act(bot, catch)`; takeover -> `seat.auto = True`;
move -> `_bot_move`; next_round -> `Game.next_round()`); the SDK then calls `changed()`.
Errors are logged and the loop sleeps 1 s.

`_bot_move`: autopilot people play at level `normal`. Plays every move from `bots.decide`;
if one raises `IllegalMove` (a bot bug; tests guard against it) it logs a warning and tries
fallbacks `accept`, `pass`, `draw`, then each playable card with suit `sun`, so the table
never stalls. A partially applied list (e.g. `call` succeeded, `play` failed) is not rolled
back.

### Catching a missed Last call

`_watch_exposure()` runs in every `changed()`. When a new seat becomes `exposed`, each bot
(other than the exposed seat) that passes `bots.wants_to_catch(level)` gets a catch time
`changed_at + bot_delay * uniform(0.7, 2.0)`. When exposure clears, `catchers` empties.
Catches sort before moves at equal times, so with `bot_delay = 0` (tests) a catch wins the
race against the next bot move.

### Autopilot

A person's seat goes `auto` when it's their turn and either `idle_turn` passed since the last
room change (`changed_at`: any change at the table, not the start of their turn and not their
own last request) or they've had no contact (`seen`, refreshed by every `state`/`act`/`join`)
for `away_after`. Non-current people aren't marked auto in advance (`away` is only a display
flag until their turn). A person who leaves a started game is on autopilot through `left`.
The seat comes back on `reclaim`, any successful game move by that person, `join` with their
token or by who's who (the page joins on load and on becoming visible), or `lobby`.

Round transitions: anyone seated can press Next round. If every person is auto, left or away,
the driver starts the next round itself; `auto_next_round` has no env var. `game_over` is
never driven: rematch/lobby stay with the host.

## Registry (`make_rooms()` -> `amrooms.Rooms`)

`Rooms(GateRoom, max_rooms=GATECHANGE_MAX_ROOMS (200), idle=GATECHANGE_ROOM_IDLE (7200),
seed=GATECHANGE_SEED)`. With a seed, each room's `rng` is seeded `"{seed}-{code}"`. Tests
change `gc.rooms.max_rooms` / `gc.rooms.idle` on the instance.

- `get(code)`: upper-cases; `RoomError(404)` "No game with that code (it may have ended)."
  for unknown or closed rooms. `find(code)` returns None instead.
- `create(public=...)`: sweeps; while at `max_rooms`, closes and drops the least recently
  touched room that is closed or not `playing()` (lobby or `game_over`); a game in progress
  is **never** evicted, and if every room is one it raises `RoomError(503)` "Every table on
  this hotspot (N) has a game going. Try again when one finishes." Picks an unused 4-letter
  code. `app.create` then seats the caller with `join(name, who=who)`; for solo it adds
  `SOLO_BOTS` (3) normal bots and starts (never public, default options).
- `sweep()`: drops closed rooms, rooms untouched for `idle`, empty lobbies (no person who
  hasn't left) at once, and empty games once untouched for 60 s (`empty_grace`), so the last
  person can still come back. It starts drivers for the rest. Runs every 5 s
  (`sweep_forever` in `app.py`'s lifespan), on `create` and `open_lobbies`, and in `app.act`
  after a `leave`. `_plan` returns None while `empty()`, so bots don't play on for nobody
  and an abandoned game goes 60 s after the last person left
  (`test_a_table_everyone_left_pauses_then_goes`).
- `open_lobbies()`: public rooms still in the lobby with a free seat and at least one person,
  newest-touched first, max 30.
