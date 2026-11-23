"""Gate Change's rooms: the game on top of amrooms (the platform's Rooms SDK).

amrooms does the seats, tokens, host, leave/rejoin, presence, long-poll and the driver loop.
This file adds the table options, starting games, the moves, and what the driver does:
bot turns after a short human-feeling delay, bots catching a missed Last call, and autopilot.

A person goes on autopilot when it's their turn and they're away (no request for AWAY_AFTER s)
or IDLE_TURN s have passed since the table last changed. Any move by that person, "reclaim",
or rejoining with their token takes the seat back. A person who leaves a started game is on
autopilot until they rejoin; when every person has left, the table pauses (and the registry
forgets it a minute later, unless someone rejoins). Between rounds, if every person is away, on autopilot or gone,
the driver starts the next round after AUTO_NEXT_ROUND s.
"""
import logging
import random
import time

from amrooms import Room, RoomError, Rooms, env_float

import bots
from engine import ALL_CARDS, SUITS, Game, IllegalMove

log = logging.getLogger("gatechange")

SOLO_BOTS = 3
GAME_MOVES = {"play", "draw", "pass", "accept", "challenge", "call", "catch"}


class GateRoom(Room):
    max_seats = 8
    bot_levels = bots.LEVELS
    bot_names = ("Ada", "Kofi", "Mira", "Jun", "Lou", "Ines", "Teo", "Noor", "Ravi", "Sam")
    away_after = env_float("GATECHANGE_AWAY", 45)
    poll_wait = env_float("GATECHANGE_POLL_WAIT", 20)
    bot_delay = env_float("GATECHANGE_BOT_DELAY", 1.0)     # seconds; 0 in tests
    idle_turn = env_float("GATECHANGE_IDLE_TURN", 60)
    auto_next_round = 8.0       # seconds at "Round over" before the driver moves on, if nobody's here

    def __init__(self, code, seed=None, public=True):
        super().__init__(code, seed)
        self.public = bool(public)
        self.options = {"mode": "500", "stack": False, "draw_until": False}
        self.next_delay = self.bot_delay
        self.catchers = []                    # [(when, bot seat)] for the current exposure
        self.seen_exposed = None

    def playing(self):
        return self.game is not None and self.game.phase != "game_over"

    def changed(self):
        super().changed()
        self.next_delay = self.bot_delay * self.rng.uniform(0.75, 1.3)
        self._watch_exposure()

    @staticmethod
    def on_autopilot(s):
        return s.kind == "bot" or s.auto or s.left

    # ---------- games ----------
    def start(self):
        if len(self.seats) < 2:
            raise IllegalMove("Add a bot or wait for someone to join: you need 2 players.")
        rules = {"stack": self.options["stack"], "draw_until": self.options["draw_until"],
                 "target": 500 if self.options["mode"] == "500" else 0}
        self.game = Game(len(self.seats), rules, random.Random(self.rng.random()))
        for s in self.seats:
            s.auto = s.left
        self.catchers, self.seen_exposed = [], None
        self.ensure_driver()

    # ---------- moves from people ----------
    def act(self, token, data):
        """Apply one request from a person. Call with self.lock held."""
        i = self.need_seat(token)
        me = self.seats[i]
        me.seen = time.monotonic()
        kind = data.get("type")
        is_host = me is self.host

        if kind == "leave":
            self.leave(token)
            return
        if self.game is None:
            if not is_host:
                raise IllegalMove("Only the host can change the table.")
            if kind == "add_bot":
                self.add_bot(data.get("level", "normal"))
            elif kind == "remove":
                self.remove_seat(data.get("seat"))
            elif kind == "set_level":
                self.set_bot_level(data.get("seat"), data.get("level"))
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

    # ---------- what one person sees ----------
    def view(self, token):
        i = self.seen(token)
        now = time.monotonic()
        g = self.game
        seats = []
        for k, s in enumerate(self.seats):
            info = self.seat_info(k, i, now)
            info["away"] = self.idle(k, now)  # here: "hasn't been seen", apart from auto/left
            del info["you"]
            seats.append(info)
        return {
            "code": self.code,
            "version": self.version,
            "state": g.phase if g else "lobby",
            "you": i,
            "is_host": self.is_host(i),
            "public": self.public,
            "options": dict(self.options),
            "seats": seats,
            "game": g.view(i) if g else None,
        }

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
        self.catchers = [(self.changed_at + self.bot_delay * self.rng.uniform(0.7, 2.0), b)
                         for b, s in enumerate(self.seats)
                         if s.kind == "bot" and b != exposed and bots.wants_to_catch(s.level, self.rng)]

    def plan(self, now):
        p = self._plan(now)
        return None if p is None else (p[0], lambda: self._execute(time.monotonic()))

    def _plan(self, now):
        g = self.game
        if g is None or self.closed or self.empty():
            return None           # everyone left: the table waits (and is forgotten after a minute)
        if g.phase == "round_over":
            # Anyone seated can press Next round. If no person is here to do it (all away, on
            # autopilot or gone), move on by ourselves after a short pause.
            here = [s.seen + self.away_after for s in self.seats
                    if s.kind == "human" and not s.auto and not s.left]
            return (max([self.changed_at + self.auto_next_round, *here]), 3, "next_round", None)
        if g.phase != "playing":
            return None
        items = [(at, 0, "catch", b) for at, b in self.catchers]
        cur = g.current
        s = self.seats[cur]
        if self.on_autopilot(s):
            at = self.changed_at + self.next_delay
            if g.exposed is not None and self.bot_delay > 0 and any(
                    x.kind == "human" and not x.auto and not x.left and k != g.exposed
                    for k, x in enumerate(self.seats)):
                at += 1.5 * self.bot_delay      # give people a moment to catch a missed Last call
            items.append((at, 1, "move", cur))
        else:
            items.append((min(self.changed_at + self.idle_turn, s.seen + self.away_after), 2, "takeover", cur))
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
        elif kind == "next_round":
            g.next_round()
        else:
            self._bot_move(seat)

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


def make_rooms():
    import os
    return Rooms(GateRoom, max_rooms=int(os.getenv("GATECHANGE_MAX_ROOMS", "200")),
                 idle=env_float("GATECHANGE_ROOM_IDLE", 2 * 3600), seed=os.getenv("GATECHANGE_SEED"))


__all__ = ["GateRoom", "RoomError", "make_rooms", "SOLO_BOTS"]
