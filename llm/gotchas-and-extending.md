# Gotchas and how to extend

## Gotchas and quirks (as the code stands)

Engine:
- **`playable` returns indexes**, not cards. The page sorts the hand for display but keeps
  the server index; anything new that refers to cards in hand should do the same.
- **A wild's top card only matches by the chosen suit**; a number can't match a wild by rank.
- **The round ends the instant a hand empties**: a final +2/+4/skip has no effect, and a
  final card never leaves its player exposed.
- **Blocked round** needs `passes >= n` consecutive "nothing to draw" turns (only possible
  when draw pile and discard-below-top are both empty). Winner is the lowest **points**, not
  fewest cards.
- **`voids` is public knowledge by design** (anyone saw you draw on that suit) but it's not
  sent in `view()`; only bots use it. It's cleared for a seat when that seat is *given*
  cards (+2, +4, catches), not on a voluntary draw.
- **Challenge "guilty" ignores wilds**: holding a Standby or another +4 doesn't make a +4
  a bluff; only a card of the previous airline does.
- **Event log is capped at 80.** Clients only see the last 80; the page's `S.lastN` logic and
  `lastSuitBeforeW4` assume the interesting events are recent.

Rooms and driver:
- **Single process only.** Rooms, locks and Events are in-process. Don't add `--workers`.
- **Room mechanics live in `amrooms`** (`../airplanemode/sdk/rooms`). A change to seats,
  leaving, presence, the driver loop or eviction is an SDK release, a pin bump in
  `requirements.txt` and an app release (Passport uses it too). Game rules stay here.
- **Room eviction never kills a live game**: at `rooms.max_rooms` (200) creating a room
  closes the least recently touched lobby or finished (`game_over`) room. A game in progress
  (`playing` or `round_over`) is never evicted; if all rooms are live, `create` raises
  `RoomError(503)` (page: error toast). A lobby with people waiting *can* be evicted.
- **Sweeping**: a background task (`rooms.sweep_forever()`, every 5 s, started in `app.py`'s
  lifespan) plus `create`, the open-games list and every `leave`. An empty lobby goes at
  once. A game everyone left is kept until nothing has changed for 60 s, but bots keep
  playing its seats, so in practice it lives until `game_over` + 60 s (or 2 h idle). A
  closed room is invisible (`get` answers 404) even before it's swept.
- **Who's who** (`whois`, platform decision 0021): a seat takes the person's name (when none
  is typed) and emoji, and the same person id finds its seat without a token. A rejoin with a
  who's who answer also renames the seat to that name. It's a label only; tests set
  `ADMIN_URL=""` and replace `gc.whois` to fake it.
- **`next_round`**: any seated person can press it. If every human is on autopilot, left, or
  away (no contact for `away_after`), the driver presses it `auto_next_round` (8 s, a class
  attribute, no env var) after the round ended, or once the last present person turns away,
  whichever is later.
- **The idle-turn timer restarts on any room change** (`changed_at`), e.g. another player's
  Last call or a join/rejoin, so 60 s is "since the last change", not "since your turn began".
- **Autopilot only kicks in on your turn.** `away` is just a display flag until then.
- **Rejoin reclaims**: `join` with the token (or a who's who match) clears `left` and `auto`. The page's `resume()`
  calls it on load and on every visibilitychange to visible, so a refresh or an unlocked
  phone takes the seat back. Merely polling `state` does not reclaim.
- **`_bot_move` fallback isn't atomic**: if a bot's `call` succeeded and its `play` failed,
  the call stays; the fallback then plays a card or draws.
- **A removed person's long-poll ends in 404** "This game has ended." (the page says "That
  table has closed."), same as a closed room.
- **Names aren't HTML-stripped**; every place the page prints a name must use `esc()`.
- Tests change timings by assigning class and registry attributes
  (`rooms.GateRoom.idle_turn`, `.away_after`, `.auto_next_round`, `gc.rooms.max_rooms`,
  `gc.rooms.idle`) and `bots.CATCH_CHANCE[...]`, and restore them in `finally`; the code reads
  them at call time, so that works. Env vars only matter before import.

Page:
- **The relative-URL test is strict**: no `"/api/`, `'/api/` or `` `/api/ ``, and no
  `src`/`href` starting with `http:`, `https:`, `//` or `/x`. Build URLs as `api/...`.
- **The branding test** fails on the commercial name as a whole word (case-insensitive;
  "unofficial" is fine) and if the four suit colours (`--sun` etc., 6-digit hex) land one each
  in red/yellow/green/blue hue bands. It asserts all four colours are found, so switching a
  suit colour to a non-hex form fails it until the regex is updated.
- Polls are restarted (new `gen`, after a `join`) on every visibilitychange to visible; the old loop exits
  only after its current request returns, so for up to 20 s two requests may be in flight.
  Harmless (`apply` ignores stale versions).
- The table's `tapCard` doesn't check autopilot; tapping a playable card while on autopilot
  sends the move, which (if legal) takes the seat back.
- `lastSuitBeforeW4` is a client-side reconstruction; if it's ever wrong only the
  explanation text is wrong.

Platform:
- Caddy's `handle_path` strips `/gatechange`, so routes are rooted at `/`.
- Long-poll holds a connection per phone for up to 20 s; through Caddy on the device this is
  *untested*.

## How to extend

### Add a move (CLAUDE.md checklist)
1. `engine.py`: handler that validates fully before mutating, raises `IllegalMove` with a
   player-facing sentence; wire it in `Game.act`; add a `can` flag in `Game.view`; log an
   event (no hidden info in it).
2. `rooms.py`: add the type to `GAME_MOVES`; extend `_clean_move` if it carries new fields.
3. `bots.py`: make `decide` produce it where sensible (and never produce an illegal one).
4. Page: a button in `renderActions`, text in `eventText`, maybe a toast in `handleEvents`,
   rules text in `showRules` and the README.
5. Tests: an engine test with `rig`/`illegal`, plus let `play_out` exercise it (108-card
   check after every move).

### Add a house rule
- Engine: default in `DEFAULT_RULES`, read `self.rules[...]`, echo it in `view()["rules"]`.
- Rooms: add to `Room.options`, `_options` (validate), and `start()`'s rules dict.
- Page: a `tog(...)` row in `renderLobby`, rules text. Tests for the rule and in the
  `test_bot_games_always_finish` rule mix.

### Add a lobby/host action
- `GateRoom.act` under `self.game is None` (host check already applies); validate seat args
  with the SDK's `seat_arg` (`RoomError(400)`). Raise `IllegalMove` for refusals (409) or
  `RoomError(status, message)` when another status fits; "not at this table" (403) already
  comes from `need_seat`.

### Change bot behaviour
- Keep `decide` pure and public-information only. Re-run the 400-game test and the hard vs
  easy test; adjust `test_bot_choices` if a deliberate choice changes.

### Change timing
- Bot pacing: `GateRoom.bot_delay` and the `uniform(0.75, 1.3)` jitter in `changed()`, the
  `1.5 * bot_delay` catch grace in `_plan`, the `uniform(0.7, 2.0)` catch window in
  `_watch_exposure`. Autopilot: `away_after`, `idle_turn`. Poll: `poll_wait` (keep it well
  below `away_after`, or attentive players will be marked away).
- New env var: read it with `env_float` as a `GateRoom` class attribute (or in `make_rooms()`
  for the registry), document it in `app.yaml` `env:` and the README settings table, and in
  the platform `compose.yaml` if the box needs a non-default.

### Change the look
- Keep the original identity: no commercial game name, logo, card design, or
  red/yellow/green/blue palette. Every suit needs a glyph as well as a colour. No external
  fonts/images/scripts. Keep animations inside the reduced-motion override. Retake
  `docs/screenshots/` afterwards.

### Before shipping
- `python tests/test_gatechange.py` passes; play a table in the platform dev stack at
  `/gatechange/` with two browsers plus bots; mark anything not verified on hardware as
  *untested*; ask before pushing a `v*` tag.
