# Rooms, seats, long-poll and the driver (`rooms.py`)

Everything is in memory. Restarting the container loses every table (decision 0016 accepts
this).

## Seat

`Seat` (`__slots__`): `kind` (`human`/`bot`), `name`, `level` (bots), `token`
(`secrets.token_urlsafe(18)` for humans, None for bots), `last_seen` (monotonic), `auto`
(a bot is playing for this person), `left` (pressed Leave during a game).

Names go through `clean_name`: whitespace collapsed, cut to 20 chars, non-printable chars
dropped, fallback `"Traveller"`. HTML is **not** stripped (`"Jesse <b>"` stays as is): the
page escapes on output. Duplicate human names get a suffix (`"Sam 2"`, case-insensitive).
Bots get a random unused name from `BOT_NAMES`, else `"Bot N"`.

## Room

Fields: `code`, `host` (a Seat), `seats` (ordered; index = engine seat), `public`,
`options` `{"mode": "500"|"single", "stack", "draw_until"}`, `game` (None in the lobby),
`version`, `_event`, `lock`, `rng` (seeded per room when `GATECHANGE_SEED` is set),
`touched`, `changed_at`, `next_delay`, `catchers`, `seen_exposed`, `task`, `closed`.

- `seat_of(token)`: linear scan with `secrets.compare_digest`; None for empty/unknown.
- `changed()`: `version += 1`; `touched = changed_at = now`; `next_delay = BOT_DELAY *
  uniform(0.75, 1.3)`; swap in a new Event and set the old one (wakes all waiters);
  `_watch_exposure()`.
- `close()`: `closed = True`, cancel the driver, `changed()` (wakes polls so they get 404).

### Lobby (`game is None`)

- `join(name, token)`: a known token rejoins its seat (clears `left` and `auto`, refreshes
  `last_seen`, renames if a name is given) **at any time, even mid-game**; a new person can
  only join a lobby with < 8 seats ("That game has already started." / "That table is full.").
- Host-only `act` types: `add_bot {level}`, `remove {seat}` (not the host), `set_level
  {seat, level}` (bots only), `options {mode?, stack?, draw_until?, public?}`, `start`.
  Seat args must be real ints (bools rejected). Non-host -> "Only the host can change the
  table." Any other type in the lobby -> "The game hasn't started yet."
- `start()`: needs >= 2 seats; builds `Game(len(seats), {stack, draw_until, target: 500 or
  0}, Random(room.rng.random()))`; seats that had `left` start on autopilot; starts the
  driver task if none is running (needs a running event loop).

### In a game

`act` types: `reclaim` (clear own `auto`/`left`), `next_round` (**any** seat, only in
`round_over`; the driver also does it when no person is here, see below), `rematch` (host, only in `game_over`; calls `start()` again with the same
seats), `lobby` (host; drops the game, clears `auto`, removes seats that left), and the
engine moves in `GAME_MOVES = {play, draw, pass, accept, challenge, call, catch}`. A game
move first goes through `_clean_move` (unknown card -> "Unknown card.", bad suit ->
"Unknown airline.", non-int target -> "No such player."), then `Game.act`; a successful
move by the person clears their `auto` and `left`. `changed()` runs only if nothing raised.

### Leaving (`_leave`)

- Lobby: the seat is deleted (its token stops working).
- Game: `left = auto = True`; a bot plays it; the seat stays until `lobby`.
- If the host left, the next human who hasn't left becomes host.
- If no human who hasn't left remains, `closed = True`; `app.act` then sweeps the room.

### `view(token)` (`:263`)

Refreshes the caller's `last_seen` and the room's `touched`. Returns `code, version,
state` (`lobby` or the engine `phase`), `you` (seat index), `is_host, public, options,
seats[{name, bot, level, host, auto, left, away}]`, `game` (`Game.view(you)` or None).
`away` = human with no contact for more than `AWAY_AFTER`.

`summary()` (open-games list): `code, host` (name), `players, humans, max`.

## Long-poll

`Room.wait(since)`: return at once if `since != version`; otherwise wait on the current
Event up to `POLL_WAIT` (20 s). The `state` endpoint calls `view` before waiting (marks you
present) and again after, so a person with an open page refreshes `last_seen` at least every
~20 s, well inside `AWAY_AFTER`. `since=-1` (the default) always returns immediately.

## The driver (`_drive`, `:374`)

One asyncio task per started room. Loop while not closed and `game` is set:

1. `_plan(now)` returns the earliest due item, or None when there's no game, the phase is
   `game_over`, or the room is closed. In `round_over` it returns one item
   `(when, 3, "next_round", None)` with `when = max(changed_at + AUTO_NEXT_ROUND,
   last_seen + AWAY_AFTER for each human who isn't auto or left)`, so it only falls due once
   nobody is here; re-planning picks up fresh `last_seen` from polls. In `playing`, items are
   tuples `(when, priority, kind, seat)`:
   - `catch` (priority 0): one per bot in `catchers`;
   - `move` (priority 1) if the current seat is a bot or on autopilot: `changed_at +
     next_delay`, plus `1.5 * BOT_DELAY` when someone is exposed and a non-autopilot human
     other than the exposed seat could catch them;
   - `takeover` (priority 2) if the current seat is a human not on autopilot:
     `min(changed_at + IDLE_TURN, last_seen + AWAY_AFTER)`.
2. No plan -> wait for the Event. Future plan -> wait for the Event with a timeout; if the
   Event fires, re-plan.
3. When due: under `room.lock`, `_execute` runs it (catch -> `Game.act(bot, catch)`; takeover
   -> `seat.auto = True`; move -> `_bot_move`; next_round -> `Game.next_round()`), then
   `changed()`. If the item isn't due after all (a poll moved `last_seen`), `_execute` does
   nothing and the loop re-plans.
4. Errors are logged and the loop sleeps 1 s; `CancelledError` propagates.

`_bot_move`: autopilot humans play at level `normal`. Plays every move from
`bots.decide`; if one raises `IllegalMove` (a bot bug; tests guard against it) it logs a
warning and tries fallbacks `accept`, `pass`, `draw`, then each playable card with suit
`sun`, so the table never stalls. Note a partially applied list (e.g. `call` succeeded,
`play` failed) is not rolled back.

### Catching a missed Last call

`_watch_exposure()` runs in every `changed()`. When a new seat becomes `exposed`, each bot
(other than the exposed seat) that passes `bots.wants_to_catch(level)` gets a catch time
`changed_at + BOT_DELAY * uniform(0.7, 2.0)`. When exposure clears, `catchers` empties.
Catches sort before moves at equal times, so with `BOT_DELAY=0` (tests) a catch wins the
race against the next bot move.

### Autopilot

A human seat goes `auto` when it's their turn and either `IDLE_TURN` passed since the last
room change (`changed_at`: any change at the table, not the start of their turn and not their
own last request) or they've had no contact (`last_seen`, refreshed by every
`state`/`act`/`join`) for `AWAY_AFTER`. Non-current humans aren't marked auto in advance
(`away` is only a display flag until their turn). The seat comes back on `reclaim`, any
successful game move by that person, `join` with their token (the page does this on load and
on becoming visible), or `lobby`.

Round transitions: anyone seated can press Next round. If every human is auto, left or away,
the driver starts the next round itself (see `_plan` above); `AUTO_NEXT_ROUND` is a module
constant (8 s), not an env var. `game_over` is never driven: rematch/lobby stay with the
host.

## Registry (`Rooms`)

- `get(code)`: upper-cases the code; returns None for unknown or closed rooms.
- `create(name, public, solo)`: sweeps; while at `MAX_ROOMS`, closes and drops the least
  recently touched room that is closed, in the lobby or at `game_over`; a game in progress
  (`playing`/`round_over`) is **never** evicted, and if every room is one it raises
  `RoomsFull` (app -> 503 with the message as `detail`); picks an unused random code;
  solo rooms are never public, get `SOLO_BOTS` normal bots and start at once (default
  options: to 500, no house rules).
- `sweep()`: drops closed rooms and rooms untouched for `ROOM_IDLE` (2 h). Called by
  `create`, `open_games` and `app.act` after a close. There is no background sweeper.
- `open_games()`: public rooms still in the lobby with a free seat, newest-touched first,
  max 30.
