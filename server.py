#!/usr/bin/env python3
"""Authoritative multiplayer blackjack server.

One running process holds every table. The source a player picks is the table
they join: "live" is one shared table, and each mechanic shoe (2-6) is its own
shared table. Players on different sources never share seats.

It also serves the official table, the operator pages (lab, mechanic, live),
and the shared card-source store the operator publishes to.

Run it on the host machine, then each gadget opens http://<host-ip>:8080/.
"""

import json
import os
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
SOURCES_STORE = Path("/tmp/ebj-sources.json")

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]
SEATS = 5
BUY_IN = 2500
MIN_BET = 25
MAX_BET = 1000

TURN_SECS = 45      # auto-stand an idle player's hand
INS_SECS = 30       # auto-decline an idle insurance decision
SETTLE_SECS = 25    # auto-advance to the next betting round
IDLE_SECS = 90      # vacate seats of a player who stopped polling

ROOM_IDS = ["live", "machine-2", "machine-3", "machine-4", "machine-5", "machine-6"]

STATIC_ROUTES = {
    "/": ROOT / "player" / "index.html",
    "/index.html": ROOT / "player" / "index.html",
    "/lab.html": ROOT / "backend" / "index.html",
    "/mechanic-tables.html": ROOT / "backend" / "mechanic-tables.html",
    "/player-simulation.html": ROOT / "backend" / "player-simulation.html",
}

LOCK = threading.Lock()
ROOMS = {}


# ------------------------- card helpers -------------------------

def rank_of(code):
    return "10" if code.startswith("10") else code[:-1]


def suit_of(code):
    return code[-1]


def valid_card(code):
    return isinstance(code, str) and bool(code) and rank_of(code) in RANKS and suit_of(code) in SUITS


def hand_value(cards):
    total = 0
    aces = 0
    for code in cards:
        rank = rank_of(code)
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("10", "J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def is_soft(cards):
    total = 0
    aces = 0
    for code in cards:
        rank = rank_of(code)
        if rank == "A":
            total += 11
            aces += 1
        elif rank in ("10", "J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return aces > 0 and total <= 21


def cards_from_history(history):
    out = []
    if isinstance(history, list):
        for row in history:
            code = row if isinstance(row, str) else (row.get("code") if isinstance(row, dict) else None)
            if valid_card(code):
                out.append(code)
    return out


def choose_live_sequence(player_seq, lab_seq):
    player = [c for c in player_seq if valid_card(c)] if isinstance(player_seq, list) else []
    lab = [c for c in lab_seq if valid_card(c)] if isinstance(lab_seq, list) else []
    lab_extends = (
        len(player) > 0
        and len(lab) >= len(player)
        and all(lab[i] == c for i, c in enumerate(player))
    )
    if lab_extends:
        return lab
    if player:
        return player
    return lab


# ------------------------- source store -------------------------

def read_sources():
    if SOURCES_STORE.exists():
        try:
            data = json.loads(SOURCES_STORE.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"machine": {"tables": {}}, "player": {"sequence": []}, "lab": {"sequence": []}}


def write_sources(data):
    SOURCES_STORE.write_text(json.dumps(data))


def feed_for_room(room_id):
    data = read_sources()
    if room_id == "live":
        player_seq = (data.get("player") or {}).get("sequence") or []
        lab_seq = (data.get("lab") or {}).get("sequence") or []
        return [c for c in choose_live_sequence(player_seq, lab_seq) if valid_card(c)]
    table = room_id.split("-", 1)[1]
    tables = (data.get("machine") or {}).get("tables") or {}
    shoe = tables.get(table) or tables.get(str(table)) or []
    if isinstance(shoe, list) and len(shoe) == 416 and all(valid_card(c) for c in shoe):
        return list(shoe)
    return []


# ------------------------- room model -------------------------

def empty_seat():
    return {
        "occupied": False,
        "name": "",
        "avatar": 1,
        "bankroll": 0,
        "bet": 0,
        "pairBet": 0,
        "jackpotBet": 0,
        "insurance": 0,
        "evenMoney": False,
        "hands": [],
        "badges": [],
        "lastBet": 0,
        "lastPair": 0,
        "lastJackpot": 0,
        "playerId": None,
        "pairWon": False,
        "jackpotWon": False,
        "jackpotPaid": False,
    }


def new_room(room_id):
    mode = "live" if room_id == "live" else "machine"
    table = 0 if room_id == "live" else int(room_id.split("-", 1)[1])
    return {
        "id": room_id,
        "loadMode": mode,
        "machineTable": table,
        "seats": [empty_seat() for _ in range(SEATS)],
        "dealer": [],
        "holeHidden": True,
        "phase": "betting",
        "activeSeat": None,
        "activeHand": 0,
        "insuranceAt": 0,
        "main": "The table is open",
        "sub": "Sit down and place a bet. Seats without a bet are skipped.",
        "feed": [],
        "feedIndex": 0,
        "feedSig": "",
        "pendingFeed": None,
        "version": 1,
        "deadline": 0,
        "players": {},
    }


def get_room(room_id):
    if room_id not in ROOM_IDS:
        return None
    room = ROOMS.get(room_id)
    if room is None:
        room = new_room(room_id)
        ROOMS[room_id] = room
    return room


def say(room, main, sub=""):
    room["main"] = main
    room["sub"] = sub


def hand_in_progress(room):
    return room["phase"] not in ("betting", "settled")


def betting_seats(room):
    out = []
    for i in range(SEATS - 1, -1, -1):
        seat = room["seats"][i]
        if seat["occupied"] and seat["bet"] >= MIN_BET:
            out.append(i)
    return out


def wager(seat):
    return seat["bet"] + seat["pairBet"] + seat["jackpotBet"]


def cards_left(room):
    return max(0, len(room["feed"]) - room["feedIndex"])


def draw(room):
    if room["feedIndex"] >= len(room["feed"]):
        return None
    card = room["feed"][room["feedIndex"]]
    room["feedIndex"] += 1
    return card


def commit_feed(room, cards):
    old = room["feed"]
    extended = len(old) > 0 and len(cards) >= len(old) and all(old[i] == c for i, c in enumerate(old))
    room["feed"] = list(cards)
    room["feedSig"] = "|".join(cards)
    if not extended:
        room["feedIndex"] = 0
    else:
        room["feedIndex"] = min(room["feedIndex"], len(cards))
    room["pendingFeed"] = None


def sync_feed(room):
    cards = feed_for_room(room["id"])
    sig = "|".join(cards)
    if sig == room["feedSig"]:
        return
    if hand_in_progress(room) and room["feed"]:
        room["pendingFeed"] = cards
        return
    commit_feed(room, cards)


def flush_pending_feed(room):
    if room["pendingFeed"] is not None and not hand_in_progress(room):
        commit_feed(room, room["pendingFeed"])


def bump(room):
    room["version"] += 1


# ------------------------- round flow -------------------------

def fresh_hands(room):
    room["dealer"] = []
    room["holeHidden"] = True
    for seat in room["seats"]:
        seat["hands"] = []
        seat["badges"] = []
        seat["insurance"] = 0
        seat["evenMoney"] = False
        seat["pairWon"] = False
        seat["jackpotWon"] = False
        seat["jackpotPaid"] = False


def clear_bets(seat):
    seat["bet"] = 0
    seat["pairBet"] = 0
    seat["jackpotBet"] = 0


def settle_pair(seat):
    cards = seat["hands"][0]["cards"]
    if seat["pairBet"] > 0 and len(cards) >= 2 and rank_of(cards[0]) == rank_of(cards[1]):
        seat["bankroll"] += seat["pairBet"] * 12
        seat["pairWon"] = True
        seat["badges"].append({"t": f"PAIR +{money(seat['pairBet'] * 11)}"})
    else:
        seat["pairWon"] = False


def suited_jackpot(cards):
    if len(cards) < 3:
        return False
    first = cards[:3]
    if len({suit_of(c) for c in first}) != 1:
        return False
    ranks = [rank_of(c) for c in first]
    if all(r == "7" for r in ranks):
        return True
    return "-".join(sorted(ranks)) == "6-7-8"


def settle_jackpot(seat):
    if seat["jackpotPaid"] or len(seat["hands"]) != 1:
        return
    cards = seat["hands"][0]["cards"]
    if len(cards) < 3:
        return
    seat["jackpotPaid"] = True
    if seat["jackpotBet"] > 0 and suited_jackpot(cards):
        seat["bankroll"] += seat["jackpotBet"] * 4
        seat["jackpotWon"] = True
        seat["badges"].append({"t": f"6-7-8 +{money(seat['jackpotBet'] * 3)}"})


def money(n):
    return f"${round(n):,}"


def deal_round(room):
    players = betting_seats(room)
    if not players:
        return
    flush_pending_feed(room)
    needed = len(players) * 2 + 2
    if cards_left(room) < needed:
        say(room, source_headline(room) + " is short",
            f"Only {cards_left(room)} cards left, and this hand needs {needed}.")
        return
    fresh_hands(room)
    for index in players:
        seat = room["seats"][index]
        seat["bankroll"] -= wager(seat)
        seat["lastBet"] = seat["bet"]
        seat["lastPair"] = seat["pairBet"]
        seat["lastJackpot"] = seat["jackpotBet"]
        seat["hands"] = [{"cards": [], "bet": seat["bet"], "status": "play", "result": None}]
    for _ in range(2):
        for index in players:
            card = draw(room)
            if card is None:
                break
            room["seats"][index]["hands"][0]["cards"].append(card)
        dealer_card = draw(room)
        if dealer_card is None:
            break
        room["dealer"].append(dealer_card)
    for index in players:
        settle_pair(room["seats"][index])
    up = rank_of(room["dealer"][0])
    if up == "A":
        room["phase"] = "insurance"
        room["insuranceAt"] = 0
        room["deadline"] = time.time() + INS_SECS
        ask_insurance(room)
    elif up in ("10", "J", "Q", "K"):
        finish_peek(room)
    else:
        begin_acting(room)


def ask_insurance(room):
    players = betting_seats(room)
    if room["insuranceAt"] >= len(players):
        return
    index = players[room["insuranceAt"]]
    seat = room["seats"][index]
    natural = hand_value(seat["hands"][0]["cards"]) == 21
    if natural:
        say(room, f"{seat['name']} has blackjack",
            "Even money pays 1 to 1 now. Insurance pays 2 to 1 if the dealer has blackjack.")
    else:
        say(room, f"{seat['name']}, insurance?",
            f"Insurance is {money(seat['bet'] // 2)} and pays 2 to 1.")


def current_insurance_seat(room):
    players = betting_seats(room)
    if room["insuranceAt"] >= len(players):
        return None
    return room["seats"][players[room["insuranceAt"]]]


def advance_insurance(room):
    room["insuranceAt"] += 1
    if room["insuranceAt"] >= len(betting_seats(room)):
        finish_peek(room)
    else:
        room["deadline"] = time.time() + INS_SECS
        ask_insurance(room)


def finish_peek(room):
    dealer_blackjack = hand_value(room["dealer"]) == 21
    up = rank_of(room["dealer"][0])
    checked = up == "A" or up in ("10", "J", "Q", "K")
    if checked and dealer_blackjack:
        room["holeHidden"] = False
        say(room, "Dealer blackjack", "The hole card makes 21.")
        settle_all(room)
    else:
        if checked:
            say(room, "No dealer blackjack", "Players act from the dealer's left.")
        begin_acting(room)


def begin_acting(room):
    for index in betting_seats(room):
        hand = room["seats"][index]["hands"][0]
        if hand["status"] != "even" and hand_value(hand["cards"]) == 21:
            hand["status"] = "blackjack"
    if not next_actor(room):
        play_dealer(room)


def next_actor(room):
    for index in betting_seats(room):
        seat = room["seats"][index]
        for h in range(len(seat["hands"])):
            if seat["hands"][h]["status"] == "play":
                room["phase"] = "acting"
                room["activeSeat"] = index
                room["activeHand"] = h
                room["deadline"] = time.time() + TURN_SECS
                label = f" hand {h + 1}" if len(seat["hands"]) > 1 else ""
                say(room, f"{seat['name']}{label}", "Hit, stand, double, or split.")
                return True
    return False


def after_player_action(room):
    seat = room["seats"][room["activeSeat"]]
    if seat:
        settle_jackpot(seat)
    if not next_actor(room):
        play_dealer(room)


def play_dealer(room):
    room["phase"] = "dealer"
    room["holeHidden"] = False
    live = any(
        any(hand["status"] not in ("bust", "even") for hand in room["seats"][i]["hands"])
        for i in betting_seats(room)
    )
    if live:
        while hand_value(room["dealer"]) < 17:
            card = draw(room)
            if card is None:
                break
            room["dealer"].append(card)
    settle_all(room)


def settle_all(room):
    room["holeHidden"] = False
    room["phase"] = "settled"
    room["activeSeat"] = None
    room["deadline"] = time.time() + SETTLE_SECS
    dealer_total = hand_value(room["dealer"])
    dealer_blackjack = len(room["dealer"]) == 2 and dealer_total == 21
    notes = []
    for index in betting_seats(room):
        seat = room["seats"][index]
        if seat["insurance"] > 0:
            if dealer_blackjack:
                seat["bankroll"] += seat["insurance"] * 3
                seat["badges"].append({"t": f"INSURANCE +{money(seat['insurance'] * 2)}"})
                notes.append(f"{seat['name']} insurance wins")
            else:
                notes.append(f"{seat['name']} insurance loses")
        settle_jackpot(seat)
        for hand_index, hand in enumerate(seat["hands"]):
            tag = f" hand {hand_index + 1}" if len(seat["hands"]) > 1 else ""
            if hand["status"] == "even":
                hand["result"] = {"t": "EVEN MONEY", "c": "win", "d": hand["bet"]}
                notes.append(f"{seat['name']}{tag} even money")
                continue
            total = hand_value(hand["cards"])
            natural = hand["status"] == "blackjack"
            if hand["status"] == "bust" or total > 21:
                hand["result"] = {"t": "BUST", "c": "lose", "d": -hand["bet"]}
                notes.append(f"{seat['name']}{tag} busts")
            elif natural and dealer_blackjack:
                seat["bankroll"] += hand["bet"]
                hand["result"] = {"t": "PUSH", "c": "push", "d": 0}
                notes.append(f"{seat['name']}{tag} push")
            elif natural:
                profit = int(hand["bet"] * 1.5)
                seat["bankroll"] += hand["bet"] + profit
                hand["result"] = {"t": "BLACKJACK", "c": "win", "d": profit}
                notes.append(f"{seat['name']}{tag} blackjack")
            elif dealer_blackjack or (dealer_total <= 21 and total < dealer_total):
                hand["result"] = {"t": "LOSE", "c": "lose", "d": -hand["bet"]}
                notes.append(f"{seat['name']}{tag} loses")
            elif dealer_total > 21 or total > dealer_total:
                seat["bankroll"] += hand["bet"] * 2
                hand["result"] = {"t": "WIN", "c": "win", "d": hand["bet"]}
                notes.append(f"{seat['name']}{tag} wins")
            else:
                seat["bankroll"] += hand["bet"]
                hand["result"] = {"t": "PUSH", "c": "push", "d": 0}
                notes.append(f"{seat['name']}{tag} push")
        clear_bets(seat)
    say(room, "Round complete", " · ".join(notes) or "No bets were in action.")


def next_round(room):
    fresh_hands(room)
    room["phase"] = "betting"
    room["activeSeat"] = None
    room["deadline"] = 0
    flush_pending_feed(room)
    say(room, "Place your bets", "Every seated player sets a bet. Seats without a bet sit this hand out.")


# ------------------------- timeouts + presence -------------------------

def tick(room):
    sync_feed(room)
    now = time.time()
    for _ in range(30):
        phase = room["phase"]
        if phase == "insurance" and room["deadline"] and now > room["deadline"]:
            advance_insurance(room)
            continue
        if phase == "acting" and room["deadline"] and now > room["deadline"]:
            seat = room["seats"][room["activeSeat"]]
            seat["hands"][room["activeHand"]]["status"] = "stand"
            after_player_action(room)
            continue
        if phase == "settled" and room["deadline"] and now > room["deadline"]:
            next_round(room)
            continue
        break
    # Free seats of players who stopped polling, but only while nobody is mid-hand.
    if room["phase"] in ("betting", "settled"):
        for seat in room["seats"]:
            pid = seat["playerId"]
            if seat["occupied"] and pid is not None:
                last = room["players"].get(pid, 0)
                if now - last > IDLE_SECS:
                    room["seats"][room["seats"].index(seat)] = empty_seat()


def touch(room, player_id):
    if player_id:
        room["players"][player_id] = time.time()


# ------------------------- action handlers -------------------------

def seat_owned_by(room, index, player_id):
    if index is None or index < 0 or index >= SEATS:
        return False
    seat = room["seats"][index]
    return seat["occupied"] and seat["playerId"] == player_id


def do_action(room, body):
    player_id = body.get("playerId")
    action = body.get("action")
    touch(room, player_id)
    tick(room)

    if action in (None, "poll", "heartbeat"):
        return True, ""

    if action == "sit":
        if room["phase"] not in ("betting", "settled"):
            return False, "New players sit down between hands."
        if room["phase"] == "settled":
            next_round(room)
        index = int(body.get("seat", -1))
        if index < 0 or index >= SEATS:
            return False, "Pick a seat."
        if room["seats"][index]["occupied"]:
            return False, "That seat is taken."
        name = str(body.get("name", "")).strip()[:16]
        if not name:
            return False, "Enter a name."
        avatar = int(body.get("avatar", 1))
        seat = empty_seat()
        seat["occupied"] = True
        seat["name"] = name
        seat["avatar"] = avatar if 1 <= avatar <= 6 else 1
        seat["bankroll"] = BUY_IN
        seat["playerId"] = player_id
        room["seats"][index] = seat
        say(room, f"{name} sits at seat {index + 1}", "Pick a chip and tap your betting circle.")
        bump(room)
        return True, ""

    if action == "leaveAll":
        for i in range(SEATS):
            if seat_owned_by(room, i, player_id) and room["phase"] in ("betting", "settled"):
                room["seats"][i] = empty_seat()
        bump(room)
        return True, ""

    index = body.get("seat")
    index = int(index) if index is not None else None

    if action in ("bet", "clear", "rebet", "rebuy", "leave"):
        if room["phase"] != "betting":
            return False, "Bet between hands."
        if not seat_owned_by(room, index, player_id):
            return False, "That is not your seat."
        seat = room["seats"][index]
        if action == "bet":
            spot = body.get("spot", "main")
            chip = int(body.get("chip", 0))
            if chip not in (25, 100, 500, 1000):
                return False, "Pick a chip."
            key = {"pair": "pairBet", "jackpot": "jackpotBet"}.get(spot, "bet")
            if seat[key] + chip > MAX_BET:
                return False, "Table max is $1,000 a spot."
            if wager(seat) + chip > seat["bankroll"]:
                return False, "That is more than your chips."
            seat[key] += chip
            say(room, f"Betting for {seat['name']}", "Main bet at least $25, then Deal.")
        elif action == "clear":
            clear_bets(seat)
        elif action == "rebet":
            clear_bets(seat)
            wanted = seat["lastBet"] + seat["lastPair"] + seat["lastJackpot"]
            if MIN_BET <= wanted <= seat["bankroll"]:
                seat["bet"] = seat["lastBet"]
                seat["pairBet"] = seat["lastPair"]
                seat["jackpotBet"] = seat["lastJackpot"]
        elif action == "rebuy":
            seat["bankroll"] += BUY_IN
            say(room, f"{seat['name']} rebuys", "Fresh chips added.")
        elif action == "leave":
            room["seats"][index] = empty_seat()
            say(room, "Seat open", "Another player can sit there.")
        bump(room)
        return True, ""

    if action == "deal":
        if room["phase"] != "betting":
            return False, "The hand already started."
        if not betting_seats(room):
            return False, "Place a main bet first."
        deal_round(room)
        bump(room)
        return True, ""

    if action in ("insure", "even", "decline"):
        if room["phase"] != "insurance":
            return False, "No insurance decision open."
        seat = current_insurance_seat(room)
        if seat is None:
            return False, "No insurance decision open."
        if seat["playerId"] != player_id:
            return False, "It is another player's insurance decision."
        if action == "even":
            if hand_value(seat["hands"][0]["cards"]) == 21:
                seat["bankroll"] += seat["hands"][0]["bet"] * 2
                seat["hands"][0]["status"] = "even"
                seat["evenMoney"] = True
        elif action == "insure":
            cost = seat["bet"] // 2
            if cost > 0 and seat["bankroll"] >= cost:
                seat["bankroll"] -= cost
                seat["insurance"] = cost
        advance_insurance(room)
        bump(room)
        return True, ""

    if action in ("hit", "stand", "double", "split"):
        if room["phase"] != "acting":
            return False, "It is not the acting phase."
        if room["activeSeat"] != index or not seat_owned_by(room, index, player_id):
            return False, "It is not your turn."
        seat = room["seats"][index]
        hand = seat["hands"][room["activeHand"]]
        if action == "hit":
            card = draw(room)
            if card is None:
                hand["status"] = "stand"
            else:
                hand["cards"].append(card)
                total = hand_value(hand["cards"])
                if total > 21:
                    hand["status"] = "bust"
                elif total == 21:
                    hand["status"] = "stand"
            after_player_action(room)
        elif action == "stand":
            hand["status"] = "stand"
            after_player_action(room)
        elif action == "double":
            if len(hand["cards"]) != 2 or seat["bankroll"] < hand["bet"]:
                return False, "You cannot double this hand."
            card = draw(room)
            if card is None:
                return False, "No card left to deal."
            seat["bankroll"] -= hand["bet"]
            hand["bet"] *= 2
            hand["cards"].append(card)
            hand["status"] = "bust" if hand_value(hand["cards"]) > 21 else "stand"
            after_player_action(room)
        elif action == "split":
            if len(seat["hands"]) != 1 or len(hand["cards"]) != 2:
                return False, "You cannot split this hand."
            if rank_of(hand["cards"][0]) != rank_of(hand["cards"][1]) or seat["bankroll"] < hand["bet"]:
                return False, "You cannot split this hand."
            if cards_left(room) < 2:
                return False, "Not enough cards left to split."
            seat["bankroll"] -= hand["bet"]
            second_card = hand["cards"].pop()
            aces = rank_of(hand["cards"][0]) == "A"
            hand["cards"].append(draw(room))
            second = {"cards": [second_card, draw(room)], "bet": hand["bet"], "status": "play", "result": None}
            hand["status"] = "stand" if (aces or hand_value(hand["cards"]) == 21) else "play"
            second["status"] = "stand" if (aces or hand_value(second["cards"]) == 21) else "play"
            seat["hands"].append(second)
            after_player_action(room)
        bump(room)
        return True, ""

    if action == "next":
        if room["phase"] == "settled":
            next_round(room)
            bump(room)
        return True, ""

    return False, "Unknown action."


# ------------------------- serialization -------------------------

def public_state(room):
    dealer = list(room["dealer"])
    if room["holeHidden"] and len(dealer) >= 2:
        dealer = [dealer[0]] + ["HIDDEN"] + dealer[2:]
    seats = []
    for seat in room["seats"]:
        seats.append({
            "occupied": seat["occupied"],
            "name": seat["name"],
            "avatar": seat["avatar"],
            "bankroll": seat["bankroll"],
            "bet": seat["bet"],
            "pairBet": seat["pairBet"],
            "jackpotBet": seat["jackpotBet"],
            "insurance": seat["insurance"],
            "evenMoney": seat["evenMoney"],
            "hands": seat["hands"],
            "badges": seat["badges"],
            "lastBet": seat["lastBet"],
            "lastPair": seat["lastPair"],
            "lastJackpot": seat["lastJackpot"],
            "playerId": seat["playerId"],
        })
    return {
        "id": room["id"],
        "loadMode": room["loadMode"],
        "machineTable": room["machineTable"],
        "seats": seats,
        "dealer": dealer,
        "holeHidden": room["holeHidden"],
        "phase": room["phase"],
        "activeSeat": room["activeSeat"],
        "activeHand": room["activeHand"],
        "insuranceAt": room["insuranceAt"],
        "main": room["main"],
        "sub": room["sub"],
        "shoeLeft": cards_left(room),
        "shoeLen": len(room["feed"]),
        "version": room["version"],
        "deadline": room["deadline"],
    }


def source_headline(room):
    return "Loaded from machine" if room["loadMode"] == "machine" else "Loaded from live"


# ------------------------- HTTP -------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ebj/1.0"

    def log_message(self, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        ext = path.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == "/api/sources":
            with LOCK:
                self._json(200, read_sources())
            return

        if route == "/api/room":
            room_id = (query.get("room") or ["machine-2"])[0]
            player_id = (query.get("playerId") or [None])[0]
            with LOCK:
                room = get_room(room_id)
                if room is None:
                    self._json(404, {"ok": False, "error": "No such table."})
                    return
                touch(room, player_id)
                tick(room)
                self._json(200, {"ok": True, "state": public_state(room)})
            return

        if route in STATIC_ROUTES:
            self._send_file(STATIC_ROUTES[route])
            return

        if route.startswith("/assets/"):
            name = route[len("/assets/"):]
            safe = (ROOT / "player" / "assets" / name).resolve()
            base = (ROOT / "player" / "assets").resolve()
            if str(safe).startswith(str(base)) and safe.exists():
                self._send_file(safe)
                return
            self.send_error(404)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "Bad JSON."})
            return
        if not isinstance(body, dict):
            self._json(400, {"ok": False, "error": "Bad body."})
            return

        if route == "/api/sources":
            with LOCK:
                current = read_sources()
                for key in ("machine", "player", "lab"):
                    if isinstance(body.get(key), dict):
                        current[key] = body[key]
                write_sources(current)
                self._json(200, {"ok": True})
            return

        if route == "/api/room":
            room_id = body.get("room", "machine-2")
            with LOCK:
                room = get_room(room_id)
                if room is None:
                    self._json(404, {"ok": False, "error": "No such table."})
                    return
                ok, error = do_action(room, body)
                self._json(200, {"ok": ok, "error": error, "state": public_state(room)})
            return

        self.send_error(404)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Multiplayer blackjack server.")
    parser.add_argument("port", nargs="?", type=int, default=8080)
    args = parser.parse_args()
    print(f"e-blackjack server on http://0.0.0.0:{args.port}  (official table at /)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
