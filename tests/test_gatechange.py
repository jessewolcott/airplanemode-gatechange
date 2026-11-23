"""Gate Change tests: rules engine, bots (many full games), rooms and the HTTP API.
Run: python tests/test_gatechange.py"""
import colorsys
import copy
import json
import os
import random
import re
import sys
import time
from pathlib import Path

os.environ.update(GATECHANGE_BOT_DELAY="0", GATECHANGE_POLL_WAIT="0.3")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app as gc  # noqa: E402
import logging  # noqa: E402
logging.getLogger("httpx").setLevel(logging.WARNING)
import bots  # noqa: E402
import rooms  # noqa: E402
from engine import ALL_CARDS, SUITS, Game, IllegalMove, make_deck, points, rank_of, suit_of  # noqa: E402

H = {"X-GateChange": "1"}


# ---------------- helpers ----------------
def rig(g, hands, top="sun.5", suit=None, current=0, pile=None):
    """Put a game in a known position. Unused cards go to the draw pile (or `pile`)."""
    g.hands = [list(h) for h in hands]
    g.discard = [top]
    g.suit = suit or suit_of(top)
    g.current, g.direction = current, 1
    g.turn_phase, g.stack, g.pending, g.drawn = "turn", 0, None, None
    g.armed, g.exposed, g.passes = False, None, 0
    g.voids = [set() for _ in range(g.n)]
    if pile is None:
        rest = make_deck()
        for c in [top, *(c for h in hands for c in h)]:
            if c in rest:
                rest.remove(c)
        random.Random(1).shuffle(rest)
        pile = rest
    g.draw_pile = list(pile)


def snapshot(g):
    return copy.deepcopy({k: v for k, v in g.__dict__.items() if k != "rng"})


def illegal(g, seat, move):
    before = snapshot(g)
    try:
        g.act(seat, move)
    except IllegalMove as e:
        assert snapshot(g) == before, "an illegal move changed the game"
        return str(e)
    raise AssertionError(f"accepted illegal move {move}")


def new(n=3, **rules):
    return Game(n, rules, random.Random(7))


# ---------------- deck and deal ----------------
def test_deck():
    d = make_deck()
    assert len(d) == 108
    for s in SUITS:
        mine = [c for c in d if suit_of(c) == s]
        assert len(mine) == 25 and mine.count(f"{s}.0") == 1
        for r in ("1", "9", "skip", "rev", "d2"):
            assert mine.count(f"{s}.{r}") == 2
    assert d.count("wild") == 4 and d.count("w4") == 4
    assert points("sun.7") == 7 and points("tide.skip") == 20 and points("w4") == 50 and points("wild") == 50
    assert len(set(d)) == len(ALL_CARDS) == 4 * 13 + 2


def test_deal():
    for seed in range(30):
        g = Game(8, None, random.Random(seed))
        assert all(len(h) == 7 for h in g.hands)
        assert rank_of(g.top).isdigit() and g.suit == suit_of(g.top)
        assert g.card_count() == 108 and g.current == 0 and g.turn_phase == "turn"
    for bad in (1, 9):
        try:
            Game(bad)
            raise AssertionError("bad player count accepted")
        except ValueError:
            pass


# ---------------- matching ----------------
def test_matching_and_illegal_moves():
    g = new(3)
    rig(g, [["sun.9", "tide.5", "star.3", "wild", "peak.skip"], ["sun.1"] * 3, ["tide.1"] * 3], top="sun.5")
    hand = g.hands[0]
    assert sorted(hand[i] for i in g.playable(0)) == sorted(["sun.9", "tide.5", "wild"])
    assert "match" in illegal(g, 0, {"type": "play", "card": "star.3"})
    assert "don't have" in illegal(g, 0, {"type": "play", "card": "peak.9"})
    assert "turn" in illegal(g, 1, {"type": "play", "card": "sun.1"})
    assert "airline" in illegal(g, 0, {"type": "play", "card": "wild"})
    assert "airline" in illegal(g, 0, {"type": "play", "card": "wild", "suit": "red"})
    illegal(g, 0, {"type": "fly"})
    illegal(g, 0, "play")
    illegal(g, 9, {"type": "draw"})
    illegal(g, 0, {"type": "pass"})
    illegal(g, 0, {"type": "accept"})
    illegal(g, 0, {"type": "challenge"})
    g.act(0, {"type": "play", "card": "tide.5"})          # number match changes the suit
    assert g.suit == "tide" and g.current == 1 and g.top == "tide.5"
    rig(g, [["sun.1", "sun.2"], ["peak.4", "sun.3"], ["x"]], top="wild", suit="peak", current=1, pile=["sun.1"] * 5)
    assert g.playable(1) == [0]                           # after a wild only the named suit matches
    g.act(1, {"type": "play", "card": "peak.4"})


def test_action_cards():
    g = new(4)
    rig(g, [["sun.skip", "sun.1"], ["tide.1"], ["tide.2"], ["tide.3"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.skip"})
    assert g.current == 2 and any(e["kind"] == "skip" and e["seat"] == 1 for e in g.events)

    rig(g, [["sun.rev", "sun.1"], ["tide.1"], ["tide.2"], ["tide.3"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.rev"})
    assert g.direction == -1 and g.current == 3

    rig(g, [["sun.d2", "sun.1"], ["tide.1"], ["tide.2"], ["tide.3"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.d2"})
    assert len(g.hands[1]) == 3 and g.current == 2

    two = new(2)
    rig(two, [["sun.rev", "sun.1", "sun.2"], ["tide.1"]], top="sun.5")
    two.act(0, {"type": "play", "card": "sun.rev"})
    assert two.current == 0                               # U-Turn with two players: go again
    two.act(0, {"type": "play", "card": "sun.1"})
    assert two.current == 1


def test_overbooked_challenge():
    g = new(3)
    # seat 0 holds a sun card while sun is current: a bluff
    rig(g, [["w4", "sun.2", "tide.3"], ["star.1", "star.2"], ["peak.1"]], top="sun.5")
    g.act(0, {"type": "play", "card": "w4", "suit": "star"})
    assert g.turn_phase == "w4" and g.current == 1 and g.suit == "star"
    v = g.view(1)
    assert v["can"]["challenge"] and v["can"]["accept"] and v["pending_by"] == 0
    assert "guilty" not in json.dumps(v)
    assert g.playable(1) == []
    illegal(g, 1, {"type": "play", "card": "star.1"})
    illegal(g, 1, {"type": "draw"})
    g.act(1, {"type": "challenge"})
    assert len(g.hands[0]) == 2 + 4 and len(g.hands[1]) == 2 and g.current == 1 and g.turn_phase == "turn"

    rig(g, [["w4", "tide.3"], ["star.1", "star.2"], ["peak.1"]], top="sun.5")   # honest
    g.act(0, {"type": "play", "card": "w4", "suit": "tide"})
    g.act(1, {"type": "challenge"})
    assert len(g.hands[1]) == 2 + 6 and len(g.hands[0]) == 1 and g.current == 2

    rig(g, [["w4", "tide.3"], ["star.1", "star.2"], ["peak.1"]], top="sun.5")
    g.act(0, {"type": "play", "card": "w4", "suit": "tide"})
    g.act(1, {"type": "accept"})
    assert len(g.hands[1]) == 6 and g.current == 2 and g.turn_phase == "turn"


def test_drawing():
    g = new(3)
    rig(g, [["star.1", "star.2"], ["tide.1"], ["tide.2"]], top="sun.5", pile=["peak.9", "sun.3"])
    g.act(0, {"type": "draw"})                     # draws sun.3 (last): playable
    assert g.turn_phase == "drawn" and g.drawn == "sun.3" and g.current == 0
    assert g.view(0)["drawn"] == "sun.3" and g.view(1)["drawn"] is None
    assert g.playable(0) == [2]
    illegal(g, 0, {"type": "play", "card": "star.1"})
    illegal(g, 0, {"type": "draw"})
    g.act(0, {"type": "pass"})
    assert g.current == 1 and len(g.hands[0]) == 3
    g.act(1, {"type": "draw"})                     # peak.9: not playable, turn passes
    assert g.current == 2 and g.turn_phase == "turn" and "sun" in g.voids[1]

    rig(g, [["star.1"] * 2, ["tide.1"], ["tide.2"]], top="sun.5", pile=["sun.4", "tide.9"])
    g.act(0, {"type": "draw"})                     # tide.9 is not playable: turn passes
    assert g.hands[0] == ["star.1", "star.1", "tide.9"] and g.current == 1


def test_draw_until_rule():
    g = new(3, draw_until=True)
    rig(g, [["star.1"], ["tide.1"], ["tide.2"]], top="sun.5", pile=["sun.8", "peak.1", "peak.2", "tide.3"])
    g.act(0, {"type": "draw"})
    assert g.hands[0] == ["star.1", "tide.3", "peak.2", "peak.1", "sun.8"] and g.drawn == "sun.8"
    assert [e for e in g.events if e["kind"] == "draw"][-1]["count"] == 4


def test_reshuffle_and_blocked_round():
    g = new(2)
    rig(g, [["star.1", "star.2"], ["tide.1"]], top="sun.5", pile=[])
    g.discard = ["peak.3", "peak.4", "sun.5"]
    g.act(0, {"type": "draw"})
    assert g.top == "sun.5" and len(g.discard) == 1 and len(g.hands[0]) == 3
    assert any(e["kind"] == "reshuffle" for e in g.events)
    # nothing to draw and nobody can play: after a lap the lowest hand wins
    g = new(2, target=0)
    rig(g, [["star.1", "star.9"], ["tide.1"]], top="sun.5", pile=[])
    g.act(0, {"type": "draw"})
    g.act(1, {"type": "draw"})
    assert g.phase == "game_over" and g.result["blocked"] and g.result["winner"] == 1
    assert g.scores == [0, 10]


def test_last_call():
    g = new(3)
    rig(g, [["sun.1", "sun.2"], ["sun.3", "tide.1"], ["tide.2", "tide.3"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.1"})          # forgot
    assert g.exposed == 0 and g.view(2)["can"]["catch"] == 0 and g.view(0)["can"]["catch"] is None
    illegal(g, 0, {"type": "catch", "target": 0})
    illegal(g, 2, {"type": "catch", "target": 1})
    g.act(2, {"type": "catch", "target": 0})             # anyone, out of turn
    assert len(g.hands[0]) == 3 and g.exposed is None
    illegal(g, 1, {"type": "catch", "target": 0})

    rig(g, [["sun.1", "sun.2"], ["sun.3", "tide.1"], ["tide.2", "tide.3"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.1"})
    g.act(0, {"type": "call"})                           # called just after: safe
    assert g.exposed is None
    illegal(g, 2, {"type": "catch", "target": 0})

    rig(g, [["sun.1", "sun.2"], ["sun.3", "tide.1"], ["tide.2", "tide.3"]], top="sun.5")
    assert g.view(0)["can"]["call"] and not g.view(1)["can"]["call"]
    g.act(0, {"type": "call"})                           # called ahead
    assert g.armed and not g.can_call(0)
    g.act(0, {"type": "play", "card": "sun.1"})
    assert g.exposed is None

    rig(g, [["sun.1", "sun.2"], ["sun.3", "tide.1"], ["tide.2", "tide.3"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.1"})
    g.act(1, {"type": "play", "card": "sun.3"})          # next player acted: the window closed
    illegal(g, 2, {"type": "catch", "target": 0})
    illegal(g, 0, {"type": "call"})
    rig(g, [["sun.1", "sun.2", "sun.4"], ["x"], ["x"]], top="sun.5")
    illegal(g, 0, {"type": "call"})                       # three cards: too early


def test_stacking_rule():
    g = new(3, stack=True)
    rig(g, [["sun.d2", "sun.1"], ["tide.d2", "tide.1"], ["star.1", "star.2"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.d2"})
    assert g.turn_phase == "stack" and g.stack == 2 and g.current == 1
    assert "+2" in illegal(g, 1, {"type": "play", "card": "tide.1"})
    illegal(g, 1, {"type": "draw"})
    g.act(1, {"type": "play", "card": "tide.d2"})
    assert g.stack == 4 and g.current == 2
    g.act(2, {"type": "accept"})
    assert len(g.hands[2]) == 6 and g.current == 0 and g.stack == 0 and g.turn_phase == "turn"


def test_scoring_and_rounds():
    g = new(3)
    rig(g, [["sun.1"], ["tide.7", "w4"], ["star.skip", "peak.0"]], top="sun.5")
    g.act(0, {"type": "play", "card": "sun.1"})
    assert g.phase == "round_over" and g.result["winner"] == 0 and g.result["points"] == 7 + 50 + 20
    assert g.scores == [77, 0, 0]
    illegal(g, 1, {"type": "draw"})
    v = g.view(1)
    assert v["result"]["hands"][2] == ["star.skip", "peak.0"]
    g.next_round()
    assert g.round == 2 and g.current == 1 and g.phase == "playing" and g.scores == [77, 0, 0]
    try:
        g.next_round()
        raise AssertionError("next_round during play")
    except IllegalMove:
        pass
    g.scores = [480, 0, 0]
    rig(g, [["tide.1"], ["tide.9"] * 3, ["sun.2"]], top="tide.5", current=0)
    g.act(0, {"type": "play", "card": "tide.1"})
    assert g.phase == "game_over" and g.winner == 0 and g.scores[0] == 480 + 27 + 2

    single = new(2, target=0)
    rig(single, [["sun.1"], ["sun.2"]], top="sun.5")
    single.act(0, {"type": "play", "card": "sun.1"})
    assert single.phase == "game_over" and single.winner == 0


def test_view_hides_secrets():
    g = Game(4, None, random.Random(3))
    for seat in range(4):
        v = g.view(seat)
        assert v["hand"] == g.hands[seat] and v["counts"] == [7] * 4
        text = json.dumps(v)
        assert "draw_pile" not in text and "hands" not in text and "guilty" not in text
    spectator = g.view(None)
    assert spectator["hand"] is None and spectator["playable"] == [] and not spectator["can"]["call"]


# ---------------- bots: many full games ----------------
def play_out(g, levels, rng, catch_rate=0.5, limit=20000):
    """Bots play a whole match. Returns the number of moves."""
    moves = 0
    while g.phase != "game_over":
        if g.phase == "round_over":
            g.next_round()
            continue
        seat = g.current
        for move in bots.decide(g, seat, levels[seat], rng):
            g.act(seat, move)                 # an IllegalMove here is a bot bug: fail loudly
            moves += 1
            assert g.card_count() == 108, "cards appeared or vanished"
            if g.phase != "playing":
                break
            if g.exposed is not None and rng.random() < catch_rate:
                catcher = rng.choice([s for s in range(g.n) if s != g.exposed])
                g.act(catcher, {"type": "catch", "target": g.exposed})
        assert moves < limit, "game did not end"
    return moves


def test_bot_games_always_finish():
    games = moves = 0
    t = time.time()
    for seed in range(400):
        rng = random.Random(seed)
        n = 2 + seed % 7
        rules = {"stack": seed % 3 == 0, "draw_until": seed % 4 == 0, "target": 0 if seed % 5 else 500}
        g = Game(n, rules, random.Random(seed * 31))
        levels = [bots.LEVELS[(seed + i) % 3] for i in range(n)]
        moves += play_out(g, levels, rng)
        games += 1
        assert g.winner is not None and max(g.scores) == g.scores[g.winner]
        if rules["target"]:
            assert g.scores[g.winner] >= 500
    print(f"     {games} bot games, {moves} moves, {time.time() - t:.1f}s")


def test_bot_games_are_repeatable():
    def run(seed):
        g = Game(5, {"target": 0}, random.Random(seed))
        play_out(g, ["hard", "normal", "easy", "hard", "normal"], random.Random(seed))
        return g.scores, g.seq
    assert run(42) == run(42)


def test_hard_bot_is_better():
    wins = {"hard": 0, "easy": 0}
    for seed in range(300):
        g = Game(2, {"target": 0}, random.Random(seed))
        levels = ["hard", "easy"] if seed % 2 else ["easy", "hard"]
        play_out(g, levels, random.Random(seed), catch_rate=0)
        wins[levels[g.winner]] += 1
    print(f"     hard vs easy, 300 heads-up rounds: {wins}")
    assert wins["hard"] > wins["easy"]


def test_bot_choices():
    rng = random.Random(1)
    g = new(3)
    # hard bot: next player has one card -> hit them with +2
    rig(g, [["sun.d2", "sun.9", "sun.8"], ["tide.1"], ["x", "y", "z"]], top="sun.5")
    assert bots.decide(g, 0, "hard", rng) == [{"type": "play", "card": "sun.d2"}]
    # normal bot holds the wild when it has a match and names its biggest suit
    rig(g, [["wild", "sun.3", "x"], ["a", "b", "c"], ["d", "e", "f"]], top="sun.5")
    assert bots.decide(g, 0, "normal", rng)[0]["card"] == "sun.3"
    rig(g, [["wild", "tide.1", "tide.2", "peak.3"], ["a"] * 5, ["d"] * 5], top="sun.5")
    move = bots.decide(g, 0, "normal", rng)[-1]
    assert move == {"type": "play", "card": "wild", "suit": "tide"}
    # nothing to play -> draw; two cards -> call first
    rig(g, [["star.1", "star.2"], ["a"] * 5, ["d"] * 5], top="sun.5")
    assert bots.decide(g, 0, "normal", rng) == [{"type": "draw"}]
    rig(g, [["sun.1", "star.2"], ["a"] * 5, ["d"] * 5], top="sun.5")
    assert bots.decide(g, 0, "hard", rng) == [{"type": "call"}, {"type": "play", "card": "sun.1"}]
    # hard challenges a +4 from a big hand (likely a bluff), not from a player known to be out of the suit
    rig(g, [["w4"] + ["sun.%d" % i for i in range(1, 9)], ["tide.1", "tide.2"], ["x"]], top="sun.5")
    g.act(0, {"type": "play", "card": "w4", "suit": "tide"})
    assert bots.decide(g, 1, "hard", rng) == [{"type": "challenge"}]
    rig(g, [["w4"] + ["tide.%d" % i for i in range(1, 9)], ["tide.1", "tide.2"], ["x"]], top="sun.5")
    g.voids[0].add("sun")
    g.act(0, {"type": "play", "card": "w4", "suit": "tide"})
    assert bots.decide(g, 1, "hard", rng) == [{"type": "accept"}]


# ---------------- rooms and API ----------------
def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.text)
    return r.json()


def hdr(token):
    return {**H, "X-Player": token}


def state(code, token, since=-1):
    return ok(c.get(f"/api/rooms/{code}/state", params={"since": since}, headers={"X-Player": token}))


def act(code, token, **move):
    return c.post(f"/api/rooms/{code}/act", json=move, headers=hdr(token))


def wait_until(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.01)
    raise AssertionError("timed out")


def my_move(v):
    """A simple legal move for a person, from their own view."""
    g = v["game"]
    can = g["can"]
    if can["call"] and g["exposed"] == v["you"]:
        return {"type": "call"}
    if can["challenge"]:
        return {"type": "accept"}
    if g["playable"]:
        card = g["hand"][g["playable"][0]]
        move = {"type": "play", "card": card}
        if card in ("wild", "w4"):
            move["suit"] = "tide"
        return move
    if can["accept"]:
        return {"type": "accept"}
    if can["pass"]:
        return {"type": "pass"}
    if can["draw"]:
        return {"type": "draw"}
    return None


def test_page_and_health():
    assert ok(c.get("/health")) == {"ok": True}
    html = c.get("/").text
    assert "Gate Change" in html and ('"api/rooms' in html or "'api/rooms" in html or "`api/rooms" in html)
    assert '"/api/' not in html and "'/api/" not in html and "`/api/" not in html
    assert not re.search(r'(src|href)="(https?:|//|/[^/])', html), re.findall(r'(?:src|href)="[^"]*"', html)
    assert c.get("/icon.svg").headers["content-type"].startswith("image/svg")


def test_page_has_airplanemode_home_on_every_screen():
    html = c.get("/").text
    home = re.compile(r'<a class="am-home[^"]*" href="\.\./"[^>]*aria-label="AirplaneMode home"')
    for screen in ("home", "lobby", "table"):
        m = re.search(rf'<section id="{screen}".*?</section>', html, re.S)
        assert m and home.search(m.group(0)), f"no AirplaneMode home link on #{screen}"
    # coming back from the menu (or the back/forward cache) rejoins the stored seat
    assert "resume()" in html and '"pageshow"' in html


def hue_family(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, _, _ = colorsys.rgb_to_hls(r, g, b)
    h *= 360
    if h < 20 or h >= 340:
        return "red"
    if 45 <= h < 70:
        return "yellow"
    if 90 <= h < 160:
        return "green"
    if 200 <= h < 250:
        return "blue"
    return f"other {h:.0f}"


def test_page_keeps_its_own_identity():
    html = c.get("/").text
    # the commercial game's name never appears as a word ("unordered", "unofficial" are fine)
    assert not re.search(r"\buno\b", html, re.I), re.findall(r".{20}\buno\b.{20}", html, re.I)
    # the four airline colours aren't the classic red/yellow/green/blue set
    colours = dict(re.findall(r"--(sun|tide|star|peak):\s*#([0-9a-fA-F]{6})\b", html))
    assert sorted(colours) == ["peak", "star", "sun", "tide"], colours
    families = {hue_family(v) for v in colours.values()}
    assert families != {"red", "yellow", "green", "blue"}, colours
    assert hue_family("ff0000") == "red" and hue_family("0000ff") == "blue"   # the check itself


def test_header_and_tokens_required():
    assert c.post("/api/rooms", json={"name": "x"}).status_code == 403
    code = ok(c.post("/api/rooms", json={"name": "Host"}, headers=H))["code"]
    assert c.get(f"/api/rooms/{code}/state").status_code == 403
    assert c.get(f"/api/rooms/{code}/state", headers={"X-Player": "nope"}).status_code == 403
    assert act(code, "nope", type="start").status_code == 403
    assert c.get("/api/rooms/ZZZZ/state", headers={"X-Player": "x"}).status_code == 404
    assert c.post(f"/api/rooms/{code}/act", content="[1]", headers=hdr("x")).status_code == 400


def test_lobby_and_multiplayer_game():
    host = ok(c.post("/api/rooms", json={"name": "  Jesse <b> "}, headers=H))
    code, ht = host["code"], host["token"]
    assert re.fullmatch(r"[A-HJ-NP-Z]{4}", code) and "I" not in code and "O" not in code
    assert any(r["code"] == code for r in ok(c.get("/api/rooms"))["rooms"])
    guest = ok(c.post(f"/api/rooms/{code.lower()}/join", json={"name": "Jesse <b>"}, headers=H))
    gt = guest["token"]
    v = state(code, gt)
    assert v["state"] == "lobby" and v["you"] == 1 and not v["is_host"]
    assert [s["name"] for s in v["seats"]] == ["Jesse <b>", "Jesse <b> 2"]
    assert act(code, gt, type="add_bot").status_code == 409            # only the host
    assert act(code, ht, type="add_bot", level="genius").status_code == 409
    ok(act(code, ht, type="add_bot", level="easy"))
    v = ok(act(code, ht, type="add_bot", level="hard"))
    assert [s["bot"] for s in v["seats"]] == [False, False, True, True]
    v = ok(act(code, ht, type="set_level", seat=2, level="normal"))
    assert v["seats"][2]["level"] == "normal"
    assert act(code, ht, type="set_level", seat=1, level="hard").status_code == 409
    assert act(code, ht, type="remove", seat=0).status_code == 409
    assert act(code, ht, type="remove", seat=True).status_code == 409
    v = ok(act(code, ht, type="remove", seat=3))
    assert len(v["seats"]) == 3
    v = ok(act(code, ht, type="options", mode="single", stack=True, draw_until=1, public=False))
    assert v["options"] == {"mode": "single", "stack": True, "draw_until": True} and not v["public"]
    assert not any(r["code"] == code for r in ok(c.get("/api/rooms"))["rooms"])
    assert act(code, ht, type="draw").status_code == 409
    ok(act(code, ht, type="start"))
    assert c.post(f"/api/rooms/{code}/join", json={"name": "Late"}, headers=H).status_code == 409

    # state is per player: your own hand only
    hv, gv = state(code, ht), state(code, gt)
    assert hv["game"]["hand"] != gv["game"]["hand"] or hv["game"]["hand"] == []
    room = gc.rooms.get(code)
    assert hv["game"]["hand"] == room.game.hands[0] and gv["game"]["hand"] == room.game.hands[1]
    assert "hands" not in json.dumps(hv["game"]) and hv["game"]["rules"] == {"stack": True, "draw_until": True}

    # an illegal move is refused and changes nothing
    v = state(code, ht)
    not_mine = gt if v["game"]["current"] == 0 else ht
    before = room.version
    r = act(code, not_mine, type="draw")
    assert r.status_code == 409 and "turn" in r.json()["detail"] and room.version == before
    assert act(code, ht, type="play", card="purple.99").status_code == 409

    # both people play until the round ends; the bot plays itself
    tokens = [ht, gt]
    for _ in range(3000):
        v = state(code, ht)
        if v["state"] != "playing":
            break
        cur = v["game"]["current"]
        if cur in (0, 1):
            mv = state(code, tokens[cur])
            move = my_move(mv)
            if move:
                act(code, tokens[cur], **move)
        else:
            time.sleep(0.002)
    v = state(code, gt)
    assert v["state"] == "game_over", v["state"]
    assert v["game"]["result"]["winner"] is not None and len(v["game"]["result"]["hands"]) == 3
    assert act(code, gt, type="rematch").status_code == 409             # host only
    v = ok(act(code, ht, type="rematch"))
    assert v["state"] == "playing" and v["game"]["round"] == 1
    v = ok(act(code, ht, type="lobby"))
    assert v["state"] == "lobby" and len(v["seats"]) == 3


def test_long_poll():
    r = ok(c.post("/api/rooms", json={"name": "A"}, headers=H))
    code, t = r["code"], r["token"]
    v = state(code, t)
    t0 = time.time()
    again = state(code, t, since=v["version"])           # nothing changes: waits, same version
    assert again["version"] == v["version"] and time.time() - t0 >= 0.25
    t0 = time.time()
    state(code, t, since=v["version"] - 1)               # already moved: at once
    assert time.time() - t0 < 0.25


def test_solo_game_vs_bots():
    r = ok(c.post("/api/rooms", json={"name": "Solo", "solo": True}, headers=H))
    code, t = r["code"], r["token"]
    v = state(code, t)
    assert v["state"] == "playing" and len(v["seats"]) == 4 and all(s["bot"] for s in v["seats"][1:])
    assert not any(x["code"] == code for x in ok(c.get("/api/rooms"))["rooms"])
    rounds = 0
    for _ in range(6000):
        v = state(code, t)
        if v["state"] == "game_over":
            break
        if v["state"] == "round_over":
            rounds += 1
            ok(act(code, t, type="next_round"))
            continue
        move = my_move(v) if v["game"]["current"] == 0 or v["game"]["exposed"] == 0 else None
        if move:
            r = act(code, t, **move)
            assert r.status_code in (200, 409), r.text   # 409: a bot moved first (e.g. caught us)
        else:
            time.sleep(0.002)
    assert v["state"] == "game_over" and max(v["game"]["scores"]) >= 500
    print(f"     solo game to 500 over {rounds + 1} rounds")


def test_rejoin_leave_and_host_handover():
    r = ok(c.post("/api/rooms", json={"name": "Host"}, headers=H))
    code, ht = r["code"], r["token"]
    gt = ok(c.post(f"/api/rooms/{code}/join", json={"name": "Guest"}, headers=H))["token"]
    again = ok(c.post(f"/api/rooms/{code}/join", json={"name": "Guest", "token": gt}, headers=H))
    assert again["token"] == gt and len(state(code, gt)["seats"]) == 2
    ok(act(code, ht, type="leave"))
    v = state(code, gt)
    assert v["is_host"] and len(v["seats"]) == 1
    ok(act(code, gt, type="leave"))
    assert c.get(f"/api/rooms/{code}/state", headers={"X-Player": gt}).status_code == 404


def test_autopilot_and_reclaim():
    r = ok(c.post("/api/rooms", json={"name": "Sleepy"}, headers=H))
    code, t = r["code"], r["token"]
    ok(act(code, t, type="add_bot", level="easy"))
    room = gc.rooms.get(code)
    old = rooms.IDLE_TURN
    rooms.IDLE_TURN = 0.2
    try:
        ok(act(code, t, type="start"))
        wait_until(lambda: room.game.current == 0)
        wait_until(lambda: room.seats[0].auto, 3)            # idle on their turn: autopilot
        assert state(code, t)["seats"][0]["auto"]
        wait_until(lambda: room.game.current != 0 or room.game.phase != "playing")
        v = ok(act(code, t, type="reclaim"))
        assert not v["seats"][0]["auto"]
        # rejoining with your token (a refresh, an unlocked phone) also takes the seat back
        room.seats[0].auto = True
        again = ok(c.post(f"/api/rooms/{code}/join", json={"name": "Sleepy", "token": t}, headers=H))
        assert again["token"] == t and not room.seats[0].auto
        assert not state(code, t)["seats"][0]["auto"]
    finally:
        rooms.IDLE_TURN = old


def test_bots_catch_a_missed_last_call():
    r = ok(c.post("/api/rooms", json={"name": "Forgetful"}, headers=H))
    code, t = r["code"], r["token"]
    ok(act(code, t, type="add_bot", level="hard"))
    ok(act(code, t, type="add_bot", level="hard"))
    ok(act(code, t, type="start"))
    room = gc.rooms.get(code)
    wait_until(lambda: room.game.current == 0 and room.game.turn_phase == "turn")
    old = bots.CATCH_CHANCE["hard"]
    bots.CATCH_CHANCE["hard"] = 1.0
    try:
        rig(room.game, [["sun.1", "sun.2"], ["tide.1"] * 5, ["tide.2"] * 5], top="sun.5")
        ok(act(code, t, type="play", card="sun.1"))
        wait_until(lambda: any(e["kind"] == "catch" for e in room.game.events))
        v = state(code, t)
        assert len(v["game"]["hand"]) == 3
        catch = [e for e in v["game"]["events"] if e["kind"] == "catch"][-1]
        assert catch["target"] == 0 and catch["seat"] in (1, 2)
    finally:
        bots.CATCH_CHANCE["hard"] = old


def test_next_round_when_nobody_is_here():
    r = ok(c.post("/api/rooms", json={"name": "Napper"}, headers=H))
    code, t = r["code"], r["token"]
    ok(act(code, t, type="add_bot", level="easy"))
    ok(act(code, t, type="start"))                         # first to 500: rounds go on
    room = gc.rooms.get(code)
    wait_until(lambda: room.game.current == 0 and room.game.turn_phase == "turn")
    old = rooms.AWAY_AFTER, rooms.AUTO_NEXT_ROUND
    rooms.AWAY_AFTER, rooms.AUTO_NEXT_ROUND = 0.5, 0.05
    try:
        rig(room.game, [["sun.1"], ["tide.1", "tide.2"]], top="sun.5")
        ok(act(code, t, type="play", card="sun.1"))
        assert room.game.phase == "round_over" and room.game.round == 1
        time.sleep(0.25)                                   # the person is still here: wait for them
        assert room.game.phase == "round_over"
        wait_until(lambda: room.game.round >= 2, 3)        # then they're away: the table moves on
    finally:
        rooms.AWAY_AFTER, rooms.AUTO_NEXT_ROUND = old
    room.close()
    # on autopilot counts as not here too: a whole match to 500 plays out with nobody pressing
    r = ok(c.post("/api/rooms", json={"name": "Autopilot"}, headers=H))
    code, t = r["code"], r["token"]
    ok(act(code, t, type="add_bot", level="easy"))
    room = gc.rooms.get(code)
    old_idle = rooms.IDLE_TURN
    rooms.AUTO_NEXT_ROUND, rooms.IDLE_TURN = 0.01, 0.05    # the driver puts them on autopilot
    try:
        ok(act(code, t, type="start"))
        wait_until(lambda: room.seats[0].auto, 3)
        wait_until(lambda: room.game.phase == "game_over", 20)
        assert room.game.round >= 2 and room.seats[0].auto
    finally:
        rooms.AUTO_NEXT_ROUND, rooms.IDLE_TURN = old[1], old_idle
        room.close()


def test_full_server_never_closes_a_live_game():
    for room in gc.rooms.rooms.values():
        room.close()
    gc.rooms.rooms.clear()
    old_max = rooms.MAX_ROOMS
    rooms.MAX_ROOMS = 3
    new_room = lambda **kw: c.post("/api/rooms", json={"name": "P", **kw}, headers=H)
    try:
        lobby = ok(new_room())["code"]
        live = [ok(new_room(solo=True))["code"] for _ in range(2)]
        third = ok(new_room())["code"]                     # the lobby goes, not a game
        assert lobby not in gc.rooms.rooms and all(x in gc.rooms.rooms for x in live)
        live.append(ok(new_room(solo=True))["code"])       # again: the other lobby goes
        assert third not in gc.rooms.rooms and sorted(gc.rooms.rooms) == sorted(live)
        r = new_room()                                     # all three are games in progress
        assert r.status_code == 503 and "game going" in r.json()["detail"], r.text
        assert sorted(gc.rooms.rooms) == sorted(live)
        gc.rooms.rooms[live[1]].game.phase = "game_over"   # a finished game can go
        ok(new_room())
        assert live[1] not in gc.rooms.rooms and live[0] in gc.rooms.rooms and live[2] in gc.rooms.rooms
    finally:
        rooms.MAX_ROOMS = old_max


def test_rooms_are_capped_and_expire():
    old_max, old_idle = rooms.MAX_ROOMS, rooms.ROOM_IDLE
    rooms.MAX_ROOMS = 5
    try:
        for i in range(8):
            ok(c.post("/api/rooms", json={"name": f"P{i}"}, headers=H))
        assert len(gc.rooms.rooms) <= 5
        rooms.ROOM_IDLE = -1
        gc.rooms.sweep()
        assert len(gc.rooms.rooms) == 0
    finally:
        rooms.MAX_ROOMS, rooms.ROOM_IDLE = old_max, old_idle


if __name__ == "__main__":
    failed = 0
    with TestClient(gc.app) as c:
        for name, fn in list(globals().items()):
            if name.startswith("test_"):
                try:
                    fn()
                    print("ok  ", name)
                except Exception:
                    failed += 1
                    import traceback
                    traceback.print_exc()
                    print("FAIL", name)
    print("failed:" if failed else "all passed", failed or "")
    sys.exit(1 if failed else 0)
