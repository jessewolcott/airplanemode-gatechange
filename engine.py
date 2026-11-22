"""Gate Change: the rules. Pure Python, no I/O, no clock: rooms.py drives it.

A colour-and-number shedding game themed on airports. Four airlines are the suits:

    sun  Sunward    (coral,  sun glyph)
    tide Tidewater  (teal,   wave glyph)
    star Starline   (violet, star glyph)
    peak Summit     (amber,  mountain glyph)

Deck (108): per airline one 0, two each of 1-9, two each of the three action cards
(skip = Delayed, rev = U-Turn, d2 = Extra Baggage +2), plus 4 Standby (wild) and 4
Overbooked +4 (w4). Cards are plain strings: "sun.7", "tide.skip", "wild", "w4".

Play matches the current suit or the top card's number/symbol, or is a wild. Instead of
playing you may draw one card; a playable drawn card may be played at once, otherwise the turn
passes. Overbooked +4 may always be played, but the next player can challenge it: if the
player held a card of the suit that was current, they draw 4 instead (and the challenger plays
on); otherwise the challenger draws 6 and is skipped.

Last call: going down to one card without calling it leaves you "exposed" until the next turn
action; anyone else who catches you in that window makes you draw 2. You can call ahead
(on your turn, holding two cards) or right after playing.

House rules (off by default): "stack" lets a player answer +2 with +2 (the total passes on);
"draw_until" makes a turn draw keep going until a playable card turns up.

Round end: the winner scores the cards left in the other hands (numbers at face value,
actions 20, wilds 50). target=0 means one round; otherwise the first to `target` wins.
If nobody can play and nothing is left to draw for a whole lap, the round is blocked and the
lowest hand wins it.

Design choices: the first discard is always a number card (action and wild cards turned up are
shuffled back), and a round ends the moment a hand is empty (a final action card has no effect).
"""
import random

SUITS = ("sun", "tide", "star", "peak")
NUMBERS = tuple(str(i) for i in range(10))
ACTIONS = ("skip", "rev", "d2")
WILDS = ("wild", "w4")
HAND_SIZE = 7
MAX_EVENTS = 80
DEFAULT_RULES = {"stack": False, "draw_until": False, "target": 500}


def make_deck():
    deck = []
    for s in SUITS:
        deck.append(f"{s}.0")
        for r in (*NUMBERS[1:], *ACTIONS):
            deck += [f"{s}.{r}"] * 2
    return deck + ["wild"] * 4 + ["w4"] * 4


ALL_CARDS = frozenset(make_deck())


def suit_of(card):
    return card.split(".", 1)[0] if "." in card else None


def rank_of(card):
    return card.split(".", 1)[1] if "." in card else card


def points(card):
    r = rank_of(card)
    return 50 if r in WILDS else 20 if r in ACTIONS else int(r)


class IllegalMove(Exception):
    """A move the rules don't allow. The message is meant for the player."""


class Game:
    """One match: rounds until someone reaches the target (or one round when target is 0).

    Seats are 0..n-1. Everything a player may not see (other hands, the draw pile, whether an
    Overbooked +4 was a bluff) stays here; view(seat) is what goes over the wire.
    """

    def __init__(self, n, rules=None, rng=None):
        if not 2 <= n <= 8:
            raise ValueError("Gate Change needs 2 to 8 players.")
        self.n = n
        self.rules = {**DEFAULT_RULES, **(rules or {})}
        self.rng = rng or random.Random()
        self.scores = [0] * n
        self.round = 0
        self.events = []
        self.seq = 0
        self.winner = None
        self.start_round()

    # ---------- round set-up ----------
    def start_round(self):
        if getattr(self, "phase", None) == "game_over":
            raise IllegalMove("The game is over.")
        self.round += 1
        deck = make_deck()
        self.rng.shuffle(deck)
        self.hands = [[] for _ in range(self.n)]
        for _ in range(HAND_SIZE):
            for h in self.hands:
                h.append(deck.pop())
        while True:                 # the first discard is a number card
            card = deck.pop()
            if rank_of(card) in NUMBERS:
                break
            deck.insert(self.rng.randrange(len(deck) + 1), card)
        self.draw_pile = deck
        self.discard = [card]
        self.suit = suit_of(card)
        self.direction = 1
        self.current = (self.round - 1) % self.n
        self.turn_phase = "turn"    # turn | drawn | w4 | stack
        self.stack = 0              # cards waiting for the current player (stack rule)
        self.pending = None         # the Overbooked +4 waiting for an answer
        self.drawn = None           # the playable card just drawn (drawn phase)
        self.armed = False          # current player called Last call ahead
        self.exposed = None         # seat that went to one card without calling
        self.passes = 0             # turns in a row with nothing played and nothing to draw
        self.voids = [set() for _ in range(self.n)]   # public: suits a seat drew instead of playing
        self.result = None
        self.phase = "playing"      # playing | round_over | game_over
        self.turns = 0
        self._ev("round_start", self.current, card=card, round=self.round)

    def next_round(self):
        if self.phase != "round_over":
            raise IllegalMove("The round is still going.")
        self.start_round()

    # ---------- queries ----------
    @property
    def top(self):
        return self.discard[-1]

    def next_seat(self, seat=None, k=1):
        return ((self.current if seat is None else seat) + self.direction * k) % self.n

    def is_playable(self, card):
        r = rank_of(card)
        if self.turn_phase == "stack":
            return r == "d2"
        if self.turn_phase == "w4":
            return False
        if r in WILDS:
            return True
        return suit_of(card) == self.suit or r == rank_of(self.top)

    def playable(self, seat):
        """Indexes of the cards this seat may play now."""
        if self.phase != "playing" or seat != self.current:
            return []
        hand = self.hands[seat]
        if self.turn_phase == "drawn":
            return [len(hand) - 1] if hand and hand[-1] == self.drawn else []
        return [i for i, c in enumerate(hand) if self.is_playable(c)]

    def can_call(self, seat):
        if self.phase != "playing":
            return False
        if self.exposed == seat:
            return True
        return (seat == self.current and not self.armed and len(self.hands[seat]) == 2
                and self.turn_phase in ("turn", "drawn", "stack"))

    def card_count(self):
        return sum(map(len, self.hands)) + len(self.draw_pile) + len(self.discard)

    # ---------- moves ----------
    def act(self, seat, action):
        """Apply one move. Raises IllegalMove (and changes nothing) if it isn't allowed."""
        if not isinstance(action, dict):
            raise IllegalMove("Unknown move.")
        if not (isinstance(seat, int) and 0 <= seat < self.n):
            raise IllegalMove("You're not at this table.")
        if self.phase != "playing":
            raise IllegalMove("The round is over.")
        kind = action.get("type")
        if kind == "call":
            return self._call(seat)
        if kind == "catch":
            return self._catch(seat, action.get("target"))
        handler = {"play": self._play, "draw": self._draw, "pass": self._pass,
                   "accept": self._accept, "challenge": self._challenge}.get(kind)
        if handler is None:
            raise IllegalMove("Unknown move.")
        if seat != self.current:
            raise IllegalMove("It's not your turn.")
        if kind == "play":
            return self._play(seat, action.get("card"), action.get("suit"))
        return handler(seat)

    def _play(self, seat, card, chosen):
        hand = self.hands[seat]
        if not isinstance(card, str) or card not in hand:
            raise IllegalMove("You don't have that card.")
        if self.turn_phase == "w4":
            raise IllegalMove("Answer the Overbooked +4 first: take the cards or challenge.")
        if self.turn_phase == "drawn" and card != self.drawn:
            raise IllegalMove("You can only play the card you just drew (or keep it).")
        if self.turn_phase == "stack" and rank_of(card) != "d2":
            raise IllegalMove("Only an Extra Baggage +2 can answer a +2 (or take the cards).")
        if not self.is_playable(card):
            raise IllegalMove("That card doesn't match the suit or the number.")
        r = rank_of(card)
        if r in WILDS and chosen not in SUITS:
            raise IllegalMove("Choose an airline for the wild card.")

        self.exposed = None
        prev_suit = self.suit
        if self.turn_phase == "drawn":
            hand.pop()              # the drawn card, which is last
        else:
            hand.remove(card)
        self.discard.append(card)
        self.suit = chosen if r in WILDS else suit_of(card)
        self.drawn = None
        self.passes = 0
        self.turns += 1
        self.voids[seat].discard(self.suit)
        self._ev("play", seat, card=card, suit=self.suit, left=len(hand))
        if not hand:
            return self._end_round(seat)
        if len(hand) == 1 and not self.armed:     # an early Last call was announced already
            self.exposed = seat

        if r == "skip":
            self._ev("skip", self.next_seat())
            self._advance(2)
        elif r == "rev":
            self.direction *= -1
            self._ev("reverse", seat, direction=self.direction)
            if self.n == 2:
                self._ev("skip", self.next_seat())
                self._advance(2)
            else:
                self._advance(1)
        elif r == "d2":
            if self.rules["stack"]:
                self.stack += 2
                self._advance(1)
                self.turn_phase = "stack"
            else:
                victim = self.next_seat()
                self._give(victim, 2, "d2")
                self._ev("skip", victim)
                self._advance(2)
        elif r == "w4":
            guilty = any(suit_of(c) == prev_suit for c in hand)
            self.pending = {"by": seat, "guilty": guilty, "prev_suit": prev_suit}
            self._advance(1)
            self.turn_phase = "w4"
        else:
            self._advance(1)

    def _draw(self, seat):
        if self.turn_phase == "drawn":
            raise IllegalMove("You already drew: play that card or keep it.")
        if self.turn_phase == "w4":
            raise IllegalMove("Answer the Overbooked +4 first: take the cards or challenge.")
        if self.turn_phase == "stack":
            raise IllegalMove("Take the stacked cards or answer with a +2.")
        self.exposed = None
        self.turns += 1
        suit = self.suit
        got = self._take(seat, 1)
        if self.rules["draw_until"]:
            while got and not self.is_playable(got[-1]):
                more = self._take(seat, 1)
                if not more:
                    break
                got += more
        self.voids[seat].add(suit)
        if not got:
            self.passes += 1
            self._ev("nothing", seat)
            if self.passes >= self.n:
                return self._end_round(None)
            return self._advance(1)
        self.passes = 0
        self._ev("draw", seat, count=len(got), why="turn", suit=suit)
        if self.is_playable(got[-1]):
            self.drawn = got[-1]
            self.turn_phase = "drawn"
        else:
            self._advance(1)

    def _pass(self, seat):
        if self.turn_phase != "drawn":
            raise IllegalMove("Play a card or draw one.")
        self.exposed = None
        self.turns += 1
        self._ev("pass", seat)
        self._advance(1)

    def _accept(self, seat):
        if self.turn_phase == "w4":
            self.exposed = None
            self.turns += 1
            self.pending = None
            self._give(seat, 4, "w4")
            self._advance(1)
        elif self.turn_phase == "stack":
            self.exposed = None
            self.turns += 1
            count, self.stack = self.stack, 0
            self._give(seat, count, "stack")
            self._advance(1)
        else:
            raise IllegalMove("There's nothing to take.")

    def _challenge(self, seat):
        if self.turn_phase != "w4":
            raise IllegalMove("There's no Overbooked +4 to challenge.")
        self.exposed = None
        self.turns += 1
        p, self.pending = self.pending, None
        if p["guilty"]:
            self._ev("challenge", seat, target=p["by"], won=True, suit=p["prev_suit"])
            self._give(p["by"], 4, "challenge")
            self.turn_phase = "turn"    # the challenger plays on
        else:
            self._ev("challenge", seat, target=p["by"], won=False, suit=p["prev_suit"])
            self._give(seat, 6, "challenge")
            self._advance(1)

    def _call(self, seat):
        if not self.can_call(seat):
            raise IllegalMove("Last call is for going down to one card.")
        if self.exposed == seat:
            self.exposed = None
        else:
            self.armed = True
        self._ev("call", seat)

    def _catch(self, seat, target):
        if self.exposed is None or target != self.exposed:
            raise IllegalMove("Too late: nobody to catch.")
        if seat == target:
            raise IllegalMove("Call it yourself instead.")
        self.exposed = None
        self._ev("catch", seat, target=target)
        self._give(target, 2, "caught")

    # ---------- helpers ----------
    def _advance(self, k):
        self.current = self.next_seat(k=k)
        self.turn_phase = "turn"
        self.armed = False
        self.drawn = None

    def _take(self, seat, k):
        got = []
        for _ in range(k):
            if not self.draw_pile:
                if len(self.discard) <= 1:
                    break
                rest, self.discard = self.discard[:-1], self.discard[-1:]
                self.rng.shuffle(rest)
                self.draw_pile = rest
                self._ev("reshuffle", None, count=len(rest))
            got.append(self.draw_pile.pop())
        self.hands[seat] += got
        if got and self.exposed == seat:
            self.exposed = None
        return got

    def _give(self, seat, k, why):
        got = self._take(seat, k)
        self.voids[seat].clear()        # new unknown cards
        self._ev("draw", seat, count=len(got), why=why)
        return got

    def _end_round(self, winner):
        blocked = winner is None
        if blocked:
            winner = min(range(self.n), key=lambda s: (sum(map(points, self.hands[s])), s))
        gained = sum(points(c) for s, h in enumerate(self.hands) if s != winner for c in h)
        self.scores[winner] += gained
        self.exposed = None
        self.pending = None
        self.drawn = None
        self.result = {"winner": winner, "points": gained, "blocked": blocked,
                       "hands": [list(h) for h in self.hands]}
        self._ev("round_end", winner, points=gained, blocked=blocked)
        target = self.rules["target"]
        if not target or self.scores[winner] >= target:
            self.phase = "game_over"
            self.winner = max(range(self.n), key=lambda s: (self.scores[s], s == winner))
            self._ev("game_end", self.winner)
        else:
            self.phase = "round_over"

    def _ev(self, kind, seat, **kw):
        self.seq += 1
        self.events.append({"n": self.seq, "kind": kind, "seat": seat, **kw})
        del self.events[:-MAX_EVENTS]

    # ---------- what one seat may see ----------
    def view(self, seat=None):
        me = seat if isinstance(seat, int) and 0 <= seat < self.n else None
        mine = me is not None and me == self.current and self.phase == "playing"
        ph = self.turn_phase
        v = {
            "round": self.round,
            "phase": self.phase,
            "target": self.rules["target"],
            "rules": {"stack": self.rules["stack"], "draw_until": self.rules["draw_until"]},
            "scores": list(self.scores),
            "counts": [len(h) for h in self.hands],
            "current": self.current,
            "direction": self.direction,
            "turn_phase": ph,
            "top": self.top,
            "suit": self.suit,
            "stack": self.stack,
            "pending_by": self.pending["by"] if self.pending else None,
            "exposed": self.exposed,
            "draw_count": len(self.draw_pile),
            "discard_count": len(self.discard),
            "hand": list(self.hands[me]) if me is not None else None,
            "playable": self.playable(me) if me is not None else [],
            "drawn": self.drawn if mine else None,
            "armed": self.armed if mine else False,
            "can": {
                "draw": mine and ph == "turn",
                "pass": mine and ph == "drawn",
                "accept": mine and ph in ("w4", "stack"),
                "challenge": mine and ph == "w4",
                "call": me is not None and self.can_call(me),
                "catch": self.exposed if me is not None and self.exposed not in (None, me) else None,
            },
            "result": self.result if self.phase != "playing" else None,
            "winner": self.winner,
            "events": list(self.events),
        }
        return v
