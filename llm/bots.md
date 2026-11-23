# Bots (`bots.py`)

Pure functions of the game and a `random.Random`. Same seed + same game = same choices
(`test_bot_games_are_repeatable`). Levels: `LEVELS = ("easy", "normal", "hard")`; an
unknown level is treated as `normal`.

## The public-information rule

A bot may only use what a player at the table could know: its own hand, the discard pile,
hand sizes, scores and `game.voids` (suits a seat drew on instead of playing; cleared when
that seat is given cards). The hard bot's challenge reads `game.pending["by"]` and
`["prev_suit"]` (both seen by everyone at the table), **never** `pending["guilty"]`. Keep it
that way: reading `game.hands[other]` contents or `draw_pile` would be cheating that no test
currently catches directly. (The strength test only asserts hard beats easy.)

## `decide(game, seat, level, rng)` -> list of moves

| Phase | Easy | Normal | Hard |
|---|---|---|---|
| `w4` | challenge 10% | challenge 20% | probability model (below) |
| `stack` | answer with its first `d2` 70% of the time, else accept | always answer with a `d2` if held | same as normal |
| `drawn` | keep it 25%, else play | keep a drawn **wild** 50% if hand > 3, else play | same as normal |
| `turn`, nothing playable | draw | draw | draw |
| `turn`, choice | random playable card | highest `_score` (random tie-break) | `_score` with hard extras |

Plays go through `_with_call`: if the bot holds exactly 2 cards and isn't armed, it
prepends `{"type": "call"}` unless it "forgets" (`FORGET_CALL`: easy 30%, normal 5%, hard
0%). Wilds get a suit from `_pick_suit`.

### `_score` (normal and hard)

- Wilds: -30 (hold them). `w4` +70 if hard and the next player has <= 2 cards, or if the
  next player is the "leader" and has <= 3.
- Actions: hard with next player <= 2 cards: `d2`/`skip` +60, `rev` +30 (when n > 2);
  else +25 if the next player is the leader; else -5.
- Numbers: `rank * 0.8` (dump high cards first).
- Plus `2 *` (cards it would still hold in the card's suit); hard adds +8 if the next player
  is known void in that suit.

"Leader" (`_leader`) = the other seat with the fewest cards, tie-broken by highest score.

### `_pick_suit`

Easy: random. Normal/hard: the suit it holds most (excluding the wild being played); hard
breaks ties toward a suit the next player is void in; then random.

### Hard challenge (`_challenge`)

If the +4 player is known void in the previous suit, accept. Otherwise estimate: unseen
cards = 108 - (own hand + whole discard pile); unseen of that suit = 25 - seen of that suit;
`p_one = unseen_suit / unseen_total`; `P(guilty) = 1 - (1 - p_one)^k` where k = the +4
player's hand size (after playing the +4). Challenge if > 0.6. So a +4 from a big hand gets
challenged, one from a short hand usually not.

## Catching

`wants_to_catch(level, rng)`: `CATCH_CHANCE` easy 25%, normal 60%, hard 90%. Rolled once
per bot per exposure in `Room._watch_exposure`; the timing is in `rooms.py`. Tests patch
`bots.CATCH_CHANCE["hard"] = 1.0` temporarily.

## Autopilot

Humans on autopilot are played by `decide(..., "normal", room.rng)` (see `Room._bot_move`).
Autopilot seats never catch others (only `kind == "bot"` seats get catch timers).

## What the tests check

- 400 full bot matches over 2-8 seats, mixed levels and rule combinations, each move
  applied through `Game.act` (an `IllegalMove` fails the test) with a 108-card check after
  every move and random catches; every match must end with a valid winner.
- Repeatability with a fixed seed.
- Hard beats easy over 300 heads-up rounds (no catches).
- Specific choices: hard hits a one-card next player with +2; normal keeps the wild and
  names its biggest suit; draws with nothing playable; calls with two cards; hard
  challenges a +4 from a big hand and accepts from a player known void.
