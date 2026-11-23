# The page (`static/index.html`)

One file: CSS (`:10-312`), an inline SVG sprite of glyphs (`:315-328`), three screens and a
few overlays (`:330-437`), and one `<script>` (`:439-1012`) of vanilla JS in strict mode. No
external assets, no framework, no build step. Dark only (`color-scheme: dark`). Line numbers
checked 2026-11-23 (after the who's who change).

## Look and identity

- Airlines (`AIR`): Sunward `SW` coral `#ff6b5b`, Tidewater `TW` teal `#19b3a3`, Starline
  `SL` violet `#8d6ff6`, Summit `SM` amber `#f2a531` (CSS vars `--sun/--tide/--star/--peak`
  with `-d` darks and `-t` text colours). Deliberately not red/yellow/green/blue.
- Every suited card shows its airline glyph (`#g-sun`, `#g-tide`, `#g-star`, `#g-peak`) in
  the band and the stub, plus the two-letter code: colour is never the only cue. Wilds use
  a four-colour wheel glyph (`#g-wild`) and the text STANDBY / OVERBKD.
- Cards are drawn as boarding passes (`cardHTML`, `:474`): band (glyph, mark, code), face
  (number, or action glyph `#g-skip`/`#g-rev`/`#g-d2`), stub ("Gate 7", "Delayed",
  "Baggage", ...). 6 and 9 are underlined. Each card has `role="img"` and an `aria-label`
  like "Tidewater Delayed" (card backs have none).
- Airport theme elsewhere: split-flap code display (`.flap`), "Departures on this hotspot",
  "Passengers", "NOW BOARDING <airline>", a dashed direction ring.

## Screens

`show(id)` toggles `#home`, `#lobby`, `#table` and hides the result overlay off the table.

Every screen has the **AirplaneMode home button** (`a.am-home`, `href="../"`, app contract
item 10): a fixed 44 px house at the top right of `#home` (`.am-home.float`), and the last
item in the right-hand group (`.bar .end`) of the lobby bar (after Rules) and the table bar
(after Scores). Both bars are `.bar.centred`: a grid `minmax(max-content, 1fr) auto
minmax(max-content, 1fr)`, so the title (lobby) and "ROUND n · TO 500" (table) sit exactly
centred and the side groups can never shrink below their contents (below ~360 px the middle
just shifts left instead of overflowing). It has `z-index: 35`, above the sheets (30/31) and `#result` (25), so it
stays visible and tappable over every sheet and the results; `#result` has 64 px top padding
so its box starts below it. No confirm: leaving keeps the seat (autopilot covers turns) and
coming back resumes (see `resume()` below). `test_page_has_airplanemode_home_on_every_screen`
checks each section has one.

- **Home** (`#home`): name field (stored; at load it's prefilled from `fetch("../api/me")`,
  the who's who name picked on the AirplaneMode menu, when there is one), **Play vs bots** (`startRoom(true)`), **Host a
  game** (`startRoom(false)`, public), 4-letter code + Join, the open-games list
  (`refreshOpen`, `GET api/rooms` every 4 s while home is shown), How to play. A URL hash
  `#join=ABCD` pre-fills the code (it does not auto-join).
- **Lobby** (`renderLobby`, `:629`): split-flap code, seats (the who's who emoji as the avatar when
  the seat has one, else the initial) with host/you tags, host-only
  level segmented control per bot and remove buttons, "+ Easy/Normal/Hard bot", options
  (Length one round / to 500, Stack +2 on +2, Draw until you can play, List on this
  hotspot), Start (disabled under 2 seats). Non-hosts see disabled controls and "Waiting
  for <host> to start...".
- **Table** (`renderTable`, `:721`): top bar (menu, code, round info, Scores + home), autopilot
  banner with **Take my seat back**, opponents strip in turn order after you (score, avatar (emoji or initial),
  name, card count with bump animation, state: thinking/playing/away/autopilot/left/bot
  level, "1 card!" or "Last call" flag), direction arrows + ring, draw pile (`N LEFT`,
  nudges when you have nothing playable), discard (`N PLAYED`, landing animation, `+N` or
  `+4?` badge for pending stack/+4), "NOW BOARDING" airline, status line with a hint, a
  one-line latest-event strip (tap for the full move log), the action row, the hand.
- **Overlays**: `#result` (round/game results), `#sheetRoot` (bottom sheets: menu, scores,
  log, rules, airline picker), `#toasts` (`aria-live="polite"`, max 3).

## State and storage

`S` (`:501`): `session {code, token}`, `v` (last view), `ver` (last version), `gen` (poll
generation), `lastN`/`animN` (last handled event / animated play), `busy`, `prevHand`,
`prevCounts`, `prevSuit`, `shownResult`, `homeTimer`.

`localStorage` keys (prefix `gatechange.`, all wrapped in try/catch): `name`, `session`
(current table), `tokens` (code -> token, last 10 kept, so re-joining a code reuses your
seat).

## Talking to the server

- `call(method, path, body, token)` (`:504`): always sends `X-GateChange: 1`, plus
  `X-Player`; `cache: "no-store"`; throws an Error carrying `detail` and `status`.
- `poll(gen)` (`:591`): loop while `gen === S.gen`: `GET api/rooms/{code}/state?since=S.ver`
  with `X-Player`. 403/404 -> `endSession("That table has closed.")`. Other failures back
  off `min(5 s, 0.8 s * fails)`. Each start of a poll loop bumps `S.gen`, which retires the
  previous loop at its next check (`enter`, page load with a stored session,
  `visibilitychange` back to visible and `pageshow` from the back/forward cache all restart
  it). The last three go through `resume()`,
  which first POSTs `join` with the stored token (taking the seat back from autopilot;
  errors ignored, a closed table then 404s in `poll`).
- `act(move)` (`:605`): single-flight (`S.busy`); POST `act`; `{left: true}` ends the session;
  403/404 end it; other errors toast `detail`.
- `apply(v)` ignores views older than the current one; re-renders only when `version`
  changed.
- `render()`: lobby -> `renderLobby` + show lobby; otherwise show table + `renderTable`.

## The hand (`renderHand`, `:819`)

- Sorted for display by airline then rank, wilds last (`sortHand`), but each card keeps its
  **server index** in `data-i`; `playable` is a list of indexes into the unsorted hand.
- Playable cards get `.ok` only when it's your turn and you're not on autopilot. Newly
  arrived cards animate in (`.dealt`, staggered); the just-drawn card is marked
  `.drawnone`.
- Overlap is computed from the hand width so it fits (`--ov`, min overlap 62% of a card).
  Recomputed on window resize.
- `tapCard` (`:855`): not your turn / not playable -> shake + an explanatory toast (matching
  the phase). A wild opens `chooseSuit` (sheet with the four airlines and how many of each
  you hold). Otherwise `act({type: "play", card})`. Keyboard: cards are focusable, Enter
  or Space plays.

## Actions row (`renderActions`, `:794`)

From `game.can`: **Catch <name>!** (`catch` with `target`), **Last call!** (pulses when
you're the exposed one) or a "Last call ✓" badge when armed, the +4 panel (Take 4 /
Challenge, explaining which airline matters), **Take N cards** for a stack, **Keep it** in
the drawn phase. Drawing is the draw pile button (disabled unless `can.draw` and not on
autopilot).

`lastSuitBeforeW4` (`:812`) reconstructs the suit that was showing before the +4 from the
events (second-to-last `play`/`round_start`), since the view doesn't send `prev_suit`. It's
display only; the server decides the challenge.

## Events, toasts and the log

`eventText(e)` (`:675`) turns each event kind into a sentence ("You"/"you" for your own seat,
names escaped). `handleEvents` toasts new events while playing: catches, others' Last
calls, challenge results, being delayed, taking +2/+4/stack cards, U-Turns. The first view
after entering only records the latest event number (no toast flood).

## Results (`renderResult`, `:897`)

Shown when phase isn't `playing`, once per `round-phase-version`. Title (you cleared /
someone cleared / wins the game), points line (blocked rounds explained), scores sorted
high to low with the cards left in each hand (mini cards) and a progress meter to the
target. Buttons: **Next round** (anyone seated; if no person is here the driver presses it after
`GateRoom.auto_next_round` s), or for the host at game over **Rematch** /
**Change the table** (non-hosts see "Waiting for the host"), plus **Leave**.

## Menu

Move log, Scores, How to play (`showRules`, the same text as the README with example
cards), host-only **End game for everyone** (confirm -> `lobby`), Leave the table
(confirm while playing: "A bot will take over your seat.").

## Responsiveness and motion

- Card width `--cwb` 66 px, 82 px from 720 px wide, 56 px when height <= 640 px (the ring is
  hidden then). From 900 px the table is centred at 900 px max.
- Safe-area insets respected; buttons are at least 44-48 px tall; `:focus-visible` outlines.
- `prefers-reduced-motion: reduce` cuts every animation/transition to 0.01 ms and stops
  the ring spin.

*Untested* on real phones: hand-fan touch feel, behaviour when a phone locks mid-game
(the visibilitychange restart and autopilot should cover it).
