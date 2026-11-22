"""Rooms, seats, long-polling and the bot driver. Everything is in memory.

A room has a 4-letter code, up to 8 seats (people and bots) and, once started, one engine
Game. Each person gets a secret token (kept in their browser) that identifies their seat, so a
refresh or a locked phone rejoins the same seat. State goes out per seat (Game.view), so no one
ever receives another player's cards.

Clients long-poll `state?since=<version>`: the call returns as soon as the room's version moves,
or after POLL_WAIT seconds with the same version. Each started room runs one asyncio task (the
driver) that plays bot turns after a short human-feeling delay, lets bots catch a missed Last
call, and puts a person on autopilot when they're away (no polls for AWAY_AFTER s) or sit on
their turn for IDLE_TURN s. Any move by that person (or "reclaim") takes the seat back.
"""
import asyncio
import logging
import os
import random
import secrets
import time

import bots
from engine import ALL_CARDS, SUITS, Game, IllegalMove

log = logging.getLogger("gatechange")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ"
MAX_SEATS = 8
MAX_ROOMS = int(os.getenv("GATECHANGE_MAX_ROOMS", "200"))
ROOM_IDLE = float(os.getenv("GATECHANGE_ROOM_IDLE", str(2 * 3600)))
BOT_DELAY = float(os.getenv("GATECHANGE_BOT_DELAY", "1.0"))     # seconds; 0 in tests
AWAY_AFTER = float(os.getenv("GATECHANGE_AWAY", "45"))
IDLE_TURN = float(os.getenv("GATECHANGE_IDLE_TURN", "60"))
POLL_WAIT = float(os.getenv("GATECHANGE_POLL_WAIT", "20"))
SEED = os.getenv("GATECHANGE_SEED")                              # fixed seed: repeatable games
SOLO_BOTS = 3
BOT_NAMES = ("Ada", "Kofi", "Mira", "Jun", "Lou", "Ines", "Teo", "Noor", "Ravi", "Sam")
GAME_MOVES = {"play", "draw", "pass", "accept", "challenge", "call", "catch"}


def clean_name(name, fallback="Traveller"):
    name = " ".join(str(name or "").split())[:20]
    return "".join(ch for ch in name if ch.isprintable()) or fallback


class Seat:
    __slots__ = ("kind", "name", "level", "token", "last_seen", "auto", "left")

    def __init__(self, kind, name, level="normal"):
        self.kind = kind                      # human | bot
        self.name = name
        self.level = level
        self.token = secrets.token_urlsafe(18) if kind == "human" else None
        self.last_seen = time.monotonic()
        self.auto = False                     # a bot is playing for this person
        self.left = False                     # they pressed Leave during a game


class Room:
    def __init__(self, code, host_name, public=True, seed=None):
        self.code = code
        self.host = Seat("human", clean_name(host_name))
        self.seats = [self.host]
        self.public = bool(public)
        self.options = {"mode": "500", "stack": False, "draw_until": False}
        self.game = None
        self.version = 1
        self._event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.rng = random.Random(seed)
        self.touched = time.monotonic()
        self.changed_at = time.monotonic()
        self.next_delay = BOT_DELAY
        self.catchers = []                    # [(when, bot seat)] for the current exposure
        self.seen_exposed = None
        self.task = None
        self.closed = False

    # ---------- bookkeeping ----------
    def seat_of(self, token):
        if not token:
            return None
        for i, s in enumerate(self.seats):
            if s.token and secrets.compare_digest(s.token, str(token)):
                return i
        return None

    def humans(self):
        return [s for s in self.seats if s.kind == "human"]

    def changed(self):
        self.version += 1
        self.touched = self.changed_at = time.monotonic()
        self.next_delay = BOT_DELAY * self.rng.uniform(0.75, 1.3)
        old, self._event = self._event, asyncio.Event()
        old.set()
        self._watch_exposure()

    async def wait(self, since):
        """Return when the version differs from `since`, or after POLL_WAIT seconds."""
        if since != self.version:
            return
        event = self._event
        try:
            await asyncio.wait_for(event.wait(), POLL_WAIT)
        except asyncio.TimeoutError:
            pass

    def close(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
        self.changed()

    # ---------- lobby ----------
    def join(self, name, token=None):
        i = self.seat_of(token)
        if i is not None:
            s = self.seats[i]
            s.left, s.last_seen = False, time.monotonic()
            if name:
                s.name = clean_name(name, s.name)
            self.changed()
            return s.token
        if self.game is not None:
            raise IllegalMove("That game has already started.")
        if len(self.seats) >= MAX_SEATS:
            raise IllegalMove("That table is full.")
        seat = Seat("human", self._unique(clean_name(name)))
        self.seats.append(seat)
        self.changed()
        return seat.token

    def _unique(self, name):
        taken = {s.name.lower() for s in self.seats}
        if name.lower() not in taken:
            return name
        n = 2
        while f"{name} {n}".lower() in taken:
            n += 1
        return f"{name} {n}"

    def add_bot(self, level="normal"):
        if len(self.seats) >= MAX_SEATS:
            raise IllegalMove("The table is full (8 seats).")
        if level not in bots.LEVELS:
            raise IllegalMove("Unknown difficulty.")
        taken = {s.name for s in self.seats}
        free = [n for n in BOT_NAMES if n not in taken] or [f"Bot {len(self.seats) + 1}"]
        self.seats.append(Seat("bot", self.rng.choice(free), level))

    def start(self):
        if len(self.seats) < 2:
            raise IllegalMove("Add a bot or wait for someone to join: you need 2 players.")
        rules = {"stack": self.options["stack"], "draw_until": self.options["draw_until"],
                 "target": 500 if self.options["mode"] == "500" else 0}
        self.game = Game(len(self.seats), rules, random.Random(self.rng.random()))
        for s in self.seats:
            s.auto = s.left
        self.catchers, self.seen_exposed = [], None
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._drive())

    # ---------- moves from people ----------
    def act(self, token, data):
        """Apply one request from a person. Call with self.lock held."""
        i = self.seat_of(token)
        if i is None:
            raise PermissionError("You're not at this table.")
        me = self.seats[i]
        me.last_seen = time.monotonic()
        kind = data.get("type")
        is_host = me is self.host

        if kind == "leave":
            self._leave(i)
        elif self.game is None:
            if not is_host:
                raise IllegalMove("Only the host can change the table.")
            if kind == "add_bot":
                self.add_bot(data.get("level", "normal"))
            elif kind == "remove":
                j = self._seat_arg(data)
                if self.seats[j] is self.host:
                    raise IllegalMove("The host can't be removed.")
                del self.seats[j]
            elif kind == "set_level":
                j = self._seat_arg(data)
                if self.seats[j].kind != "bot" or data.get("level") not in bots.LEVELS:
                    raise IllegalMove("Only bots have a difficulty.")
                self.seats[j].level = data["level"]
            elif kind == "options":
                self._options(data)
            elif kind == "start":
                self.start()
            else:
                raise IllegalMove("The game hasn't started yet.")
        else:
            g = self.game
            if kind == "reclaim":
                me.auto = me.left = False
            elif kind == "next_round":
                g.next_round()
            elif kind == "rematch":
                if g.phase != "game_over":
                    raise IllegalMove("The game is still going.")
                if not is_host:
                    raise IllegalMove("The host starts the rematch.")
                self.start()
            elif kind == "lobby":
                if not is_host:
                    raise IllegalMove("Only the host can end the game.")
                self.game = None
                for s in self.seats:
                    s.auto = False
                # seats that left during the game go now
                self.seats = [s for s in self.seats if not s.left]
            elif kind in GAME_MOVES:
                g.act(i, _clean_move(data))
                me.auto = me.left = False
            else:
                raise IllegalMove("Unknown move.")
        self.changed()

    def _seat_arg(self, data):
        j = data.get("seat")
        if not isinstance(j, int) or isinstance(j, bool) or not 0 <= j < len(self.seats):
            raise IllegalMove("No such seat.")
        return j

    def _options(self, data):
        if "mode" in data:
            if data["mode"] not in ("500", "single"):
                raise IllegalMove("Unknown mode.")
            self.options["mode"] = data["mode"]
        for key in ("stack", "draw_until"):
            if key in data:
                self.options[key] = bool(data[key])
        if "public" in data:
            self.public = bool(data["public"])

    def _leave(self, i):
        s = self.seats[i]
        if self.game is None:
            del self.seats[i]
        else:
            s.left = s.auto = True
        if s is self.host:
            nxt = next((x for x in self.seats if x.kind == "human" and not x.left and x is not s), None)
            if nxt:
                self.host = nxt
        if not any(x.kind == "human" and not x.left for x in self.seats):
            self.closed = True

    # ---------- what one person sees ----------
    def view(self, token):
        i = self.seat_of(token)
        now = time.monotonic()
        if i is not None:
            self.seats[i].last_seen = now
        self.touched = now
        g = self.game
        return {
            "code": self.code,
            "version": self.version,
            "state": g.phase if g else "lobby",
            "you": i,
            "is_host": i is not None and self.seats[i] is self.host,
            "public": self.public,
            "options": dict(self.options),
            "seats": [{
                "name": s.name,
                "bot": s.kind == "bot",
                "level": s.level if s.kind == "bot" else None,
                "host": s is self.host,
                "auto": s.auto,
                "left": s.left,
                "away": s.kind == "human" and now - s.last_seen > AWAY_AFTER,
            } for s in self.seats],
            "game": g.view(i) if g else None,
        }

    def summary(self):
        return {"code": self.code, "host": self.host.name, "players": len(self.seats),
                "humans": len(self.humans()), "max": MAX_SEATS}

    # ---------- the driver: bots, catches, autopilot ----------
    def _watch_exposure(self):
        g = self.game
        exposed = g.exposed if g else None
        key = (exposed, g.seq) if exposed is not None else None
        if exposed is None:
            self.catchers, self.seen_exposed = [], None
            return
        if self.seen_exposed and self.seen_exposed[0] == exposed:
            return
        self.seen_exposed = key
        # Timed from the move that exposed them, like the next bot move, so with no delay a
        # catch ties with that move and wins (catches sort first).
        self.catchers = [(self.changed_at + BOT_DELAY * self.rng.uniform(0.7, 2.0), b)
                         for b, s in enumerate(self.seats)
                         if s.kind == "bot" and b != exposed and bots.wants_to_catch(s.level, self.rng)]

    def _plan(self, now):
        g = self.game
        if g is None or g.phase != "playing" or self.closed:
            return None
        items = [(at, 0, "catch", b) for at, b in self.catchers]
        cur = g.current
        s = self.seats[cur]
        if s.kind == "bot" or s.auto:
            at = self.changed_at + self.next_delay
            if g.exposed is not None and BOT_DELAY > 0 and any(
                    x.kind == "human" and not x.auto and k != g.exposed for k, x in enumerate(self.seats)):
                at += 1.5 * BOT_DELAY           # give people a moment to catch a missed Last call
            items.append((at, 1, "move", cur))
        else:
            items.append((min(self.changed_at + IDLE_TURN, s.last_seen + AWAY_AFTER), 2, "takeover", cur))
        return min(items)

    def _execute(self, now):
        plan = self._plan(now)
        if plan is None or plan[0] > now:
            return
        _, _, kind, seat = plan
        g = self.game
        if kind == "catch":
            self.catchers = [c for c in self.catchers if c[1] != seat]
            if g.exposed is not None and g.exposed != seat:
                g.act(seat, {"type": "catch", "target": g.exposed})
        elif kind == "takeover":
            self.seats[seat].auto = True
        else:
            self._bot_move(seat)
        self.changed()

    def _bot_move(self, seat):
        g = self.game
        s = self.seats[seat]
        level = s.level if s.kind == "bot" else "normal"
        try:
            for move in bots.decide(g, seat, level, self.rng):
                g.act(seat, move)
            return
        except IllegalMove as e:          # shouldn't happen (tests check); never stall the table
            log.warning("bot move refused in %s: %s", self.code, e)
        hand = g.hands[seat]
        fallbacks = [{"type": "accept"}, {"type": "pass"}, {"type": "draw"}]
        fallbacks += [{"type": "play", "card": hand[i], "suit": SUITS[0]} for i in g.playable(seat)]
        for move in fallbacks:
            try:
                g.act(seat, move)
                return
            except IllegalMove:
                continue

    async def _drive(self):
        while not self.closed and self.game is not None:
            event = self._event
            plan = self._plan(time.monotonic())
            try:
                if plan is None:
                    await event.wait()
                    continue
                delay = plan[0] - time.monotonic()
                if delay > 0:
                    try:
                        await asyncio.wait_for(event.wait(), delay)
                        continue                       # something changed: plan again
                    except asyncio.TimeoutError:
                        pass
                async with self.lock:
                    if self.game is not None and not self.closed:
                        self._execute(time.monotonic())
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("driver error in room %s", self.code)
                await asyncio.sleep(1)


def _clean_move(data):
    move = {"type": data.get("type")}
    card = data.get("card")
    if card is not None:
        if not isinstance(card, str) or card not in ALL_CARDS:
            raise IllegalMove("Unknown card.")
        move["card"] = card
    suit = data.get("suit")
    if suit is not None:
        if suit not in SUITS:
            raise IllegalMove("Unknown airline.")
        move["suit"] = suit
    target = data.get("target")
    if target is not None:
        if not isinstance(target, int) or isinstance(target, bool):
            raise IllegalMove("No such player.")
        move["target"] = target
    return move


class Rooms:
    def __init__(self):
        self.rooms = {}

    def sweep(self):
        now = time.monotonic()
        for code, room in list(self.rooms.items()):
            if room.closed or now - room.touched > ROOM_IDLE:
                self.rooms.pop(code).close()

    def get(self, code):
        room = self.rooms.get(str(code or "").upper())
        if room is None or room.closed:
            return None
        return room

    def create(self, name, public=True, solo=False):
        self.sweep()
        while len(self.rooms) >= MAX_ROOMS:
            oldest = min(self.rooms.values(), key=lambda r: r.touched)
            self.rooms.pop(oldest.code).close()
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        while code in self.rooms:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        seed = None if SEED is None else f"{SEED}-{code}"
        room = Room(code, name, public=public and not solo, seed=seed)
        if solo:
            for _ in range(SOLO_BOTS):
                room.add_bot("normal")
            room.start()
        self.rooms[code] = room
        return room

    def open_games(self):
        self.sweep()
        rooms = [r for r in self.rooms.values()
                 if r.public and r.game is None and len(r.seats) < MAX_SEATS and not r.closed]
        rooms.sort(key=lambda r: -r.touched)
        return [r.summary() for r in rooms[:30]]
