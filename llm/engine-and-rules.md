# The rules engine (`engine.py`)

Pure Python: no I/O, no clock, randomness only from the `rng` (a `random.Random`) passed to
`Game`. `rooms.py` drives it. Line numbers below are `engine.py` as of 0.1.0.

## Cards

Cards are plain strings.

| Card | String | Count | Points |
|---|---|---|---|
| Number 0 | `"sun.0"` | 1 per airline | 0 |
| Numbers 1-9 | `"tide.7"` | 2 each per airline | face value |
| Delayed (skip) | `"star.skip"` | 2 per airline | 20 |
| U-Turn (reverse) | `"peak.rev"` | 2 per airline | 20 |
| Extra Baggage +2 | `"sun.d2"` | 2 per airline | 20 |
| Standby (wild) | `"wild"` | 4 | 50 |
| Overbooked +4 | `"w4"` | 4 | 50 |

Suits (`SUITS`): `sun` Sunward (coral, sun glyph), `tide` Tidewater (teal, waves), `star`
Starline (violet, star), `peak` Summit (amber, mountain). 25 cards per airline, 108 total.
`ALL_CARDS` is the frozenset of the 54 distinct strings (used by `rooms._clean_move`).
Helpers: `suit_of(card)` (None for wilds), `rank_of(card)` (the card itself for wilds),
`points(card)`.

## Game state (set in `start_round`, `engine.py:96-127`)

| Field | Meaning | Public? |
|---|---|---|
| `n`, `rules`, `scores`, `round`, `winner` | match-level | yes (`rules` partly: target, stack, draw_until) |
| `hands` | list of lists per seat | own hand only; counts public |
| `draw_pile` | list, top = last | count only |
| `discard` | list, top = last | top card and count |
| `suit` | current airline (a wild sets it) | yes |
| `direction` | 1 or -1 | yes |
| `current` | seat whose turn it is | yes |
| `turn_phase` | `turn` / `drawn` / `w4` / `stack` | yes |
| `stack` | cards waiting under the stack rule | yes |
| `pending` | `{"by", "guilty", "prev_suit"}` for an unanswered +4 | only `pending_by` |
| `drawn` | the playable card just drawn | only to that seat, on their turn |
| `armed` | current player called Last call ahead | only to that seat |
| `exposed` | seat that went to one card without calling | yes |
| `passes` | consecutive "nothing to draw" turns | no (internal) |
| `voids` | per seat, suits they drew on instead of playing | engine-public, not in `view()`; bots read it |
| `events` | last 80 events `{"n", "kind", "seat", ...}` | yes |
| `seq` | event counter | via events |
| `phase` | `playing` / `round_over` / `game_over` | yes |
| `result` | `{"winner", "points", "blocked", "hands"}` at round end | yes, after the round |

## Round set-up

- Deal 7 to each seat (round-robin pops from the shuffled deck).
- First discard must be a number card: action/wild cards turned up are re-inserted at a
  random position and the next card is tried.
- `current = (round - 1) % n`: the first player rotates each round. Direction resets to 1.
- `Game(n, rules, rng)` raises `ValueError` unless 2 <= n <= 8. `start_round` raises
  `IllegalMove` once the game is over; `next_round()` only works in `round_over`.

## Turn phases and what's legal

| `turn_phase` | Who | Legal moves |
|---|---|---|
| `turn` | current | `play` (matching card or wild), `draw`, `call` (holding 2, not yet armed) |
| `drawn` | current | `play` of the drawn card only, `pass` ("keep it"), `call` |
| `w4` | current (the +4 victim) | `accept` (take 4) or `challenge`; no play, no draw |
| `stack` | current (stack rule) | `play` of any `d2`, `accept` (take the total), `call` |
| any | any seat | `catch` (target = `exposed`, not yourself); `call` if you are `exposed` |

`is_playable` (`:142`): in `stack` only a `d2`; in `w4` nothing; wilds always; otherwise same
suit as `self.suit` or same rank as the top card. Note a wild's suit match uses the
*chosen* suit, and a number on a wild top only matches by suit (rank "wild" never equals a
number). `playable(seat)` returns **indexes** into the seat's hand; in `drawn` it is only the
last index.

`act(seat, action)` (`:173`): rejects non-dict actions, bad seats, and anything when
`phase != "playing"`. `call` and `catch` are allowed out of turn; everything else needs
`seat == current`. Every handler validates before mutating, so an `IllegalMove` leaves the
game unchanged (the tests snapshot `__dict__` to prove it).

## The moves

- **play** (`_play`, `:196`): card must be in hand and playable for the phase; a wild needs
  `suit` in `SUITS` ("Choose an airline for the wild card."). Removes the card (in `drawn`,
  pops the last card), pushes to discard, sets `suit` (chosen for wilds), clears
  `exposed`/`drawn`/`passes`, removes the new suit from the player's `voids`, logs `play`
  with `left`. Then:
  - empty hand -> `_end_round(seat)` immediately (a final action card has no effect);
  - one card left and not `armed` -> `exposed = seat`;
  - `skip`: event `skip` for the next seat, advance 2;
  - `rev`: flip direction; with 2 players it acts as a skip (advance 2), else advance 1;
  - `d2` without stack: next seat draws 2, `skip` event, advance 2; with stack: `stack += 2`,
    advance 1, phase `stack`;
  - `w4`: `guilty = any(card of the previous suit left in the hand)` (wilds don't count);
    `pending = {by, guilty, prev_suit}`, advance 1, phase `w4`;
  - number / wild: advance 1.
- **draw** (`_draw`, `:259`): only in `turn`. Takes 1 card; with `draw_until` keeps taking
  while the last one isn't playable and cards remain. Adds the old suit to the seat's
  `voids`. If nothing could be drawn: `passes += 1`, event `nothing`, and if `passes >= n`
  the round ends **blocked**; otherwise advance. If the last drawn card is playable, phase
  `drawn` (same player); else advance.
- **pass** (`:291`): only in `drawn`; keeps the card, advance 1.
- **accept** (`:299`): in `w4` take 4 (`why: "w4"`), in `stack` take the total
  (`why: "stack"`); both advance 1 (the victim loses the turn).
- **challenge** (`:315`): only in `w4`. Guilty -> the +4 player draws 4 and the challenger
  **plays on** (phase `turn`, `current` unchanged). Not guilty -> challenger draws 6 and is
  skipped. Event `challenge` carries `target`, `won`, `suit` (= prev_suit).
- **call** (`_call`, `:330`): if you are `exposed`, clears it (calling right after playing
  is safe); otherwise sets `armed` ("called ahead"). `can_call` (`:161`): exposed seat, or
  the current seat holding exactly 2, not armed, in `turn`/`drawn`/`stack`.
- **catch** (`_catch`, `:339`): target must equal `exposed`, catcher != target; target
  draws 2 (`why: "caught"`), exposure cleared.

The exposure window closes on the next turn action by anyone (every turn handler sets
`exposed = None`) or when the exposed seat is given cards (`_take` clears it).
`call`/`catch` don't advance the turn or clear `armed`.

## Helpers

- `_advance(k)`: move `current` k seats in `direction`; reset phase to `turn`, `armed`,
  `drawn`.
- `_take(seat, k)`: draws up to k; when the pile is empty it reshuffles all but the top
  discard (event `reshuffle`); if only the top discard is left it stops short.
- `_give(seat, k, why)`: `_take` + **clear that seat's voids** (new unknown cards) + `draw`
  event.

## Scoring and match end (`_end_round`, `:377`)

- Winner = the seat that emptied its hand, or (blocked) the seat with the lowest hand
  **points** (tie: lowest seat index).
- Winner gains the points of every other hand. `result.hands` reveals all hands (only in
  `round_over`/`game_over`).
- `target` 0 = one round -> `game_over` at once. Otherwise `game_over` when the round
  winner's score >= target, else `round_over`. `winner` = highest score (tie goes to the
  round winner).

## Events

Kinds: `round_start` (card, round), `play` (card, suit, left), `draw` (count, why =
`turn`/`d2`/`w4`/`stack`/`challenge`/`caught`, plus `suit` for turn draws), `skip`,
`reverse` (direction), `pass`, `nothing`, `call`, `catch` (target), `challenge`
(target, won, suit), `reshuffle` (count; seat None), `round_end` (points, blocked),
`game_end`. Events never name a drawn card.

## `view(seat)` (`:403`)

Keys: `round, phase, target, rules{stack, draw_until}, scores, counts, current, direction,
turn_phase, top, suit, stack, pending_by, exposed, draw_count, discard_count, hand,
playable, drawn, armed, can{draw, pass, accept, challenge, call, catch}, result, winner,
events`.

- `hand`/`playable` only for the asking seat; `seat=None` (spectator) gets `hand: None`,
  `playable: []`, no `call`.
- `drawn` and `armed` only when it's that seat's turn.
- `can.catch` is the exposed seat index (or None) when it's someone else.
- Not sent: other hands, draw pile contents, `pending.guilty`, `pending.prev_suit`, `voids`,
  `passes`. (The page reconstructs prev_suit from events; see table-page.md.)
