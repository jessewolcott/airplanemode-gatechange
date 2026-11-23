# HTTP API (`app.py`)

Paths are relative to the app root; on the box Caddy serves them under `/gatechange/`
(prefix stripped). The module docstring lists the same API.

## Conventions

- **Seat token** in the `X-Player` header. It is the only identity; there are no accounts.
- **POSTs need `X-GateChange: 1`** (`need_header`): a cross-site form or simple request
  can't set it, so other sites can't act for you. Missing -> 403 "Missing request header."
- Bodies must be a JSON object: not JSON -> 400 "Send JSON."; JSON but not an object ->
  400 "Send a JSON object."
- Errors are `{"detail": "<sentence for the player>"}` (custom `HTTPException` and
  `RoomError` handlers).
  The page shows `detail` in a toast. (FastAPI's own 422 for a non-integer `since` keeps its
  default list-shaped `detail`.)
- The SDK's `RoomError` carries its own status (403 not at the table, 404 no room, 400 bad
  lobby input, 409 refused, 503 server full); `IllegalMove` from `Room.act` -> 409.

## Endpoints

| Method | Path | Header | Body | Result |
|---|---|---|---|---|
| GET | `/health` | - | - | `{"ok": true}` |
| GET | `/` | - | - | `static/index.html`, `Cache-Control: no-cache` |
| GET | `/icon.svg` | - | - | SVG, `max-age=86400` |
| GET | `/api/rooms` | - | - | `{"rooms": [summary...]}` open public lobbies |
| POST | `/api/rooms` | X-GateChange | `{name, solo?, public?}` | `{code, token}` |
| POST | `/api/rooms/{code}/join` | X-GateChange | `{name, token?}` | `{code, token}` |
| GET | `/api/rooms/{code}/state?since=N` | X-Player | - | room view (long-poll) |
| POST | `/api/rooms/{code}/act` | both | `{type, ...}` | room view, or `{"left": true}` |

### `POST /api/rooms`

`public` defaults to true (only a literal `false` turns it off); `solo` must be literal
`true`. Solo = you + 3 normal bots, already started, never listed. The returned token is
the host's. Before creating, `whois(request)` asks the admin service who this is (who's who:
`{id, name, avatar}` or None; 1.5 s timeout, cached 30 s, off when `ADMIN_URL` is empty);
the seat takes the who's who name when the body's `name` is empty, and the emoji either way.
At `GATECHANGE_MAX_ROOMS` (200) creating evicts the least recently touched room that isn't in a
game (lobby, `game_over`, or already closed). If every room is a game in progress (playing or
`round_over`), nothing is closed and the call fails with 503 "Every table on this hotspot (N)
has a game going. Try again when one finishes." (SDK `RoomError(503)`); the page shows the
detail as an error toast.

### `POST /api/rooms/{code}/join`

Code is case-insensitive. Unknown or closed room -> 404 "No game with that code (it may
have ended)." The token can come in the body (`token`) or the `X-Player` header; a valid
token rejoins its seat (works mid-game) and clears `left` **and `auto`**: coming back takes
the seat back from autopilot. Without a token, a who's who match (same person id, e.g.
another browser on the same phone) does the same. A rejoin renames the seat when a name (or a
who's who name) comes with it. The page calls this with its stored token on load and whenever
it becomes visible again. Otherwise: a new seat in a lobby, or 409 ("That game has already
started." / "That table is full (8 seats)."). `whois` runs before the room lock; the join
runs under `room.lock`.

### `GET /api/rooms/{code}/state?since=N`

403 "You're not at this table." for a missing/unknown token (checked before waiting). Marks
the caller present, waits until `version != since` or `GateRoom.poll_wait` (20 s), then 404 "This
game has ended." if the room closed or the seat disappeared meanwhile (e.g. the host removed
you), else the caller's view. Not under the lock (no awaits inside `view`).

### `POST /api/rooms/{code}/act`

Under `room.lock`. Body `type` is one of:

| Type | When | Who | Extra fields |
|---|---|---|---|
| `leave` | any time | anyone | - |
| `add_bot` | lobby | host | `level` (default normal) |
| `remove` | lobby | host | `seat` (int, not the host) |
| `set_level` | lobby | host | `seat`, `level` |
| `options` | lobby | host | any of `mode` (`"500"`/`"single"`), `stack`, `draw_until`, `public` |
| `start` | lobby | host | - |
| `play` | game | current seat | `card`, `suit` (wilds) |
| `draw`, `pass`, `accept`, `challenge` | game | current seat | - |
| `call` | game | holder of 2 on their turn, or the exposed seat | - |
| `catch` | game | anyone but the target | `target` (seat int) |
| `reclaim` | game | yourself | - |
| `next_round` | `round_over` | anyone | - |
| `rematch` | `game_over` | host | - |
| `lobby` | game | host | - |

Returns the caller's new view, or `{"left": true}` after `leave` (then `rooms.sweep()` runs:
an empty lobby goes at once; a game everyone left stays until nothing has changed for 60 s,
so the last person can still come back). Unknown type -> 409. Bad lobby input (unknown
level, a seat that doesn't exist or is a bool, a level for a person) -> 400; full table,
removing the host -> 409.

## The room view (shape)

```json
{
  "code": "KQRT", "version": 17, "state": "lobby|playing|round_over|game_over",
  "you": 0, "is_host": true, "public": true,
  "options": {"mode": "500", "stack": false, "draw_until": false},
  "seats": [{"name": "Sam", "avatar": "🦊", "bot": false, "level": null, "host": true,
             "left": false, "auto": false, "away": false}],
  "game": null
}
```

`game` is `Game.view(you)` once started (keys in engine-and-rules.md). The page uses
`version` both as the next `since` and to ignore stale responses.

`avatar` is the who's who emoji, or null. Open-games summary: `{"code", "host", "players",
"humans", "max": 8}`.
