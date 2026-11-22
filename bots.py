"""Gate Change bots. Pure functions of the game and a random.Random (seed it for tests).

A bot only uses what a player at the table could know: its own hand, the discard pile, hand
sizes, scores, and the public `voids` (suits a seat drew on instead of playing).

    easy    plays a random legal card, sometimes forgets Last call, rarely challenges.
    normal  holds wilds back, saves actions for the leader, names the suit it holds most.
    hard    also hits a player about to go out with +2/+4/Delayed, names suits the next
            player is out of, and challenges a +4 when the odds say it was a bluff.
"""
from engine import ACTIONS, SUITS, WILDS, rank_of, suit_of

LEVELS = ("easy", "normal", "hard")
FORGET_CALL = {"easy": 0.3, "normal": 0.05, "hard": 0.0}
CATCH_CHANCE = {"easy": 0.25, "normal": 0.6, "hard": 0.9}
SUIT_SIZE = 25                                       # cards per airline


def decide(game, seat, level, rng):
    """The bot's moves for this turn, as a list of actions (a Last call may come first)."""
    level = level if level in LEVELS else "normal"
    ph = game.turn_phase
    hand = game.hands[seat]
    if ph == "w4":
        return [{"type": "challenge" if _challenge(game, seat, level, rng) else "accept"}]
    if ph == "stack":
        d2 = [c for c in hand if rank_of(c) == "d2"]
        if d2 and (level != "easy" or rng.random() < 0.7):
            return _with_call(game, seat, level, rng, {"type": "play", "card": d2[0]})
        return [{"type": "accept"}]
    if ph == "drawn":
        card = game.drawn
        if level == "easy" and rng.random() < 0.25:
            return [{"type": "pass"}]
        if level != "easy" and rank_of(card) in WILDS and len(hand) > 3 and rng.random() < 0.5:
            return [{"type": "pass"}]       # keep the wild for later
        return _with_call(game, seat, level, rng, _play(game, seat, card, level, rng))
    idx = game.playable(seat)
    if not idx:
        return [{"type": "draw"}]
    cards = [hand[i] for i in idx]
    if level == "easy":
        card = rng.choice(cards)
    else:
        card = max(cards, key=lambda c: (_score(game, seat, c, level), rng.random()))
    return _with_call(game, seat, level, rng, _play(game, seat, card, level, rng))


def wants_to_catch(level, rng):
    return rng.random() < CATCH_CHANCE.get(level, 0.6)


# ---------- helpers ----------
def _with_call(game, seat, level, rng, play):
    if len(game.hands[seat]) == 2 and not game.armed and rng.random() >= FORGET_CALL[level]:
        return [{"type": "call"}, play]
    return [play]


def _play(game, seat, card, level, rng):
    move = {"type": "play", "card": card}
    if rank_of(card) in WILDS:
        move["suit"] = _pick_suit(game, seat, card, level, rng)
    return move


def _held(hand, skip=None):
    counts = {s: 0 for s in SUITS}
    for c in hand:
        if suit_of(c):
            counts[suit_of(c)] += 1
    if skip and suit_of(skip):
        counts[suit_of(skip)] -= 1
    return counts


def _pick_suit(game, seat, card, level, rng):
    if level == "easy":
        return rng.choice(SUITS)
    held = _held([c for c in game.hands[seat]], skip=card)
    nxt_void = game.voids[game.next_seat(seat)] if level == "hard" else set()
    return max(SUITS, key=lambda s: (held[s], s in nxt_void, rng.random()))


def _leader(game, seat):
    others = [s for s in range(game.n) if s != seat]
    return min(others, key=lambda s: (len(game.hands[s]), -game.scores[s]))


def _score(game, seat, card, level):
    """How much a normal/hard bot likes playing this card now."""
    r = rank_of(card)
    nxt = game.next_seat(seat)
    nxt_cards = len(game.hands[nxt])
    against_leader = nxt == _leader(game, seat)
    danger = level == "hard" and nxt_cards <= 2
    s = 0.0
    if r in WILDS:
        s -= 30                               # hold wilds back
        if r == "w4" and (danger or (against_leader and nxt_cards <= 3)):
            s += 70
    elif r in ACTIONS:
        if danger and r in ("d2", "skip"):
            s += 60
        elif danger and r == "rev" and game.n > 2:
            s += 30
        elif against_leader:
            s += 25
        else:
            s -= 5                            # keep it for when it matters
    else:
        s += int(r) * 0.8                     # dump high numbers first
    held = _held(game.hands[seat], skip=card)
    new_suit = suit_of(card)
    if new_suit:
        s += held[new_suit] * 2               # stay in a suit we hold
        if level == "hard" and new_suit in game.voids[nxt]:
            s += 8                            # the next player is out of it
    return s


def _challenge(game, seat, level, rng):
    if level == "easy":
        return rng.random() < 0.1
    if level == "normal":
        return rng.random() < 0.2
    p = game.pending or {}
    by, suit = p.get("by"), p.get("prev_suit")
    if by is None or suit is None or suit in game.voids[by]:
        return False
    k = len(game.hands[by])
    hand = game.hands[seat]
    seen = list(hand) + list(game.discard)
    unseen_total = 108 - len(seen)
    unseen_suit = SUIT_SIZE - sum(1 for c in seen if suit_of(c) == suit)
    if unseen_total <= 0 or unseen_suit <= 0:
        return False
    p_one = unseen_suit / unseen_total
    guilty = 1 - (1 - p_one) ** k
    return guilty > 0.6
