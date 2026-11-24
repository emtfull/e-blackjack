#!/usr/bin/env python3
"""Shared multiplayer blackjack rooms.

Players who open the official table on different gadgets join the same room
when they pick the same source. Room state is stored in Upstash Redis when
KV_REST_API_URL is set, otherwise in this process for local play.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from lib.shoe import TABLES, build_shoe, normalize_profile

SOURCES_STORE = Path("/tmp/ebj-sources.json")
SOURCES_KEY = "ebj:sources"
ROOM_KEY = "ebj:room:{}"
LOCK_KEY = "ebj:lock:{}"

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]
SEATS = 5
BUY_IN = 2500
MIN_BET = 25
MAX_BET = 1000

BET_SECS = 18       # multiplayer betting window after the first main bet
SOLO_BET_SECS = 8   # shorter window when only one player is seated
BANKER_SECS = 2     # banker signal before the auto-deal
TURN_SECS = 45      # auto-stand an idle player's hand
INS_SECS = 30       # auto-decline an idle insurance decision
SETTLE_SECS = 25    # auto-advance to the next betting round
IDLE_SECS = 90      # vacate seats of a player who stopped polling
PRESENCE_SECS = 8   # drop from online/watcher counts if they stop polling

ROOM_IDS = ["live", "machine-2", "machine-3", "machine-4", "machine-5", "machine-6"]

LOCK = threading.Lock()
ROOMS = {}


def redis_configured():
    return bool(os.environ.get("KV_REST_API_URL") and os.environ.get("KV_REST_API_TOKEN"))


def redis_cmd(parts):
    """Run one Upstash REST command via POST body, e.g. ['GET', key] or ['SET', key, value]."""
    base = os.environ["KV_REST_API_URL"].rstrip("/")
    token = os.environ["KV_REST_API_TOKEN"]
    body = json.dumps(parts).encode()
    req = urllib.request.Request(
        base,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=12) as res:
        payload = json.loads(res.read().decode())
    return payload.get("result")


def redis_set(key, value, ex=None):
    if ex:
        return redis_cmd(["SET", key, value, "EX", str(ex)])
    return redis_cmd(["SET", key, value])


def redis_get(key):
    return redis_cmd(["GET", key])


def acquire_lock(room_id, tries=20):
    key = LOCK_KEY.format(room_id)
    for _ in range(tries):
        try:
            result = redis_cmd(["SET", key, "1", "NX", "EX", "5"])
            if result == "OK":
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def release_lock(room_id):
    try:
        redis_cmd(["DEL", LOCK_KEY.format(room_id)])
    except Exception:
        pass


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

def empty_sources():
    return {"machine": {"tables": {}}, "player": {"sequence": []}, "lab": {"sequence": []}}


def read_sources():
    if redis_configured():
        try:
            raw = redis_get(SOURCES_KEY)
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    if SOURCES_STORE.exists():
        try:
            data = json.loads(SOURCES_STORE.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return empty_sources()


def write_sources(data):
    encoded = json.dumps(data)
    if redis_configured():
        try:
            redis_set(SOURCES_KEY, encoded)
        except Exception:
            pass
    try:
        SOURCES_STORE.write_text(encoded)
    except OSError:
        pass


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
        "burn": 0,
        "dealUntil": 0,
        "version": 1,
        "deadline": 0,
        "timerKind": "",
        "bankerSignal": False,
        "players": {},
    }


def load_room(room_id):
    if room_id not in ROOM_IDS:
        return None
    if redis_configured():
        try:
            raw = redis_get(ROOM_KEY.format(room_id))
            if raw:
                data = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(data, dict) and data.get("id") == room_id:
                    return data
        except Exception:
            pass
        return new_room(room_id)
    room = ROOMS.get(room_id)
    if room is None:
        room = new_room(room_id)
        ROOMS[room_id] = room
    return room


def save_room(room):
    if redis_configured():
        redis_set(ROOM_KEY.format(room["id"]), json.dumps(room))
        return
    ROOMS[room["id"]] = room


def get_room(room_id):
    return load_room(room_id)


def with_room(room_id, fn):
    """Load a room, run fn(room), save it, return fn's result."""
    if room_id not in ROOM_IDS:
        return None, {"ok": False, "error": "No such table."}
    if redis_configured():
        if not acquire_lock(room_id):
            return None, {"ok": False, "error": "Table is busy. Try again."}
        try:
            room = load_room(room_id)
            result = fn(room)
            save_room(room)
            return room, result
        finally:
            release_lock(room_id)
    with LOCK:
        room = load_room(room_id)
        result = fn(room)
        save_room(room)
        return room, result


def handle_get(room_id, player_id):
    def work(room):
        touch(room, player_id)
        tick(room)
        return {"ok": True, "state": public_state(room)}

    room, result = with_room(room_id, work)
    if room is None:
        return result
    return result


def handle_post(body):
    if not isinstance(body, dict):
        return {"ok": False, "error": "Bad body."}
    room_id = body.get("room", "machine-2")

    def work(room):
        ok, error = do_action(room, body)
        return {"ok": ok, "error": error, "state": public_state(room)}

    room, result = with_room(room_id, work)
    if room is None:
        return result
    return result


def handle_sources_get():
    return read_sources()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def machine_bucket(current):
    machine = current.get("machine")
    if not isinstance(machine, dict):
        machine = {}
        current["machine"] = machine
    if not isinstance(machine.get("tables"), dict):
        machine["tables"] = {}
    if not isinstance(machine.get("meta"), dict):
        machine["meta"] = {}
    return machine


def store_built_shoes(built):
    """Merge built table shoes into the source store. built maps table number to build_shoe()."""
    current = read_sources()
    machine = machine_bucket(current)
    tables_out = {}
    meta_out = {}
    for number, shoe in built.items():
        key = str(number)
        machine["tables"][key] = list(shoe["cards"])
        machine["meta"][key] = dict(shoe["meta"])
        tables_out[key] = machine["tables"][key]
        meta_out[key] = machine["meta"][key]
    machine["updatedAt"] = _now()
    write_sources(current)
    return tables_out, meta_out


def parse_build_tables(raw):
    numbers = []
    if not isinstance(raw, list) or not raw:
        raw = list(TABLES)
    for item in raw:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number in TABLES and number not in numbers:
            numbers.append(number)
    return numbers


def build_and_store(build):
    profile = normalize_profile(build.get("profile"))
    seed = build.get("seed") or ""
    numbers = parse_build_tables(build.get("tables"))
    if not numbers:
        return {"ok": False, "error": "No table to build."}
    built = {}
    for number in numbers:
        built[number] = build_shoe(number, profile, seed)
    tables_out, meta_out = store_built_shoes(built)
    return {"ok": True, "tables": tables_out, "meta": meta_out}


def handle_sources_post(body):
    if not isinstance(body, dict):
        return {"ok": False, "error": "Bad body."}
    if isinstance(body.get("build"), dict):
        return build_and_store(body["build"])
    current = read_sources()
    for key in ("machine", "player", "lab"):
        if isinstance(body.get(key), dict):
            current[key] = body[key]
    write_sources(current)
    return {"ok": True}


def meta_for_table(table):
    data = read_sources()
    meta = ((data.get("machine") or {}).get("meta") or {})
    info = meta.get(str(table)) or meta.get(table) or {}
    return info if isinstance(info, dict) else {}


def limits_for_room(room):
    """Burn index and the first index that is not dealt. Live shoes have no cut."""
    if room.get("id") == "live":
        return 0, len(room.get("feed") or [])
    table = str(room.get("id", "")).split("-", 1)[-1]
    info = meta_for_table(table)
    burn = int(info.get("burn") or 0)
    deal_until = info.get("dealUntil")
    if deal_until is None:
        deal_until = len(room.get("feed") or [])
    else:
        deal_until = int(deal_until)
    return burn, deal_until


def apply_limits(room):
    burn, deal_until = limits_for_room(room)
    room["burn"] = burn
    room["dealUntil"] = deal_until
    if room.get("phase") in ("betting", "settled") and room.get("feedIndex", 0) < burn:
        room["feedIndex"] = burn


def profile_for_table(table):
    return normalize_profile(meta_for_table(table).get("profile"))


def open_machine_shoe(room, needed):
    """Skip the burn. When the next hand would pass the cut, build a new shoe first."""
    apply_limits(room)
    deal_until = room.get("dealUntil") or 0
    if room.get("feed") and room["feedIndex"] + needed <= deal_until:
        return True
    if hand_in_progress(room):
        return False
    table = int(str(room["id"]).split("-", 1)[1])
    shoe = build_shoe(table, profile_for_table(table), "")
    store_built_shoes({table: shoe})
    commit_feed(room, shoe["cards"])
    return room["feedIndex"] + needed <= (room.get("dealUntil") or 0)


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


def occupied_seats(room):
    return [i for i, seat in enumerate(room["seats"]) if seat["occupied"]]


def all_seated_ready(room):
    seated = occupied_seats(room)
    if not seated:
        return False
    return all(room["seats"][i]["bet"] >= MIN_BET for i in seated)


def wager(seat):
    return seat["bet"] + seat["pairBet"] + seat["jackpotBet"]


def clear_bet_timer(room):
    if room["phase"] == "betting":
        room["deadline"] = 0
        room["timerKind"] = ""
        room["bankerSignal"] = False


def arm_bet_timer(room):
    """Start or refresh the betting countdown after a main bet is in play."""
    if room["phase"] != "betting" or not betting_seats(room):
        clear_bet_timer(room)
        return
    seated = len(occupied_seats(room))
    window = SOLO_BET_SECS if seated <= 1 else BET_SECS
    now = time.time()
    if not room["deadline"] or room["timerKind"] not in ("betting", "banker"):
        room["deadline"] = now + window
        room["timerKind"] = "betting"
        room["bankerSignal"] = False
        say(
            room,
            "Betting open",
            f"Banker Elias deals in {window}s, or sooner when every seated player has a main bet.",
        )
    if all_seated_ready(room):
        # Everyone ready: banker signal, then deal.
        room["deadline"] = min(room["deadline"] or (now + BANKER_SECS), now + BANKER_SECS)
        room["timerKind"] = "banker"
        room["bankerSignal"] = True
        say(room, "Banker signal", "No more bets. Dealer Elias is dealing.")


def banker_deal(room):
    if room["phase"] != "betting":
        return False
    if not betting_seats(room):
        clear_bet_timer(room)
        say(room, "Place your bets", "The banker waits for at least one main bet.")
        return False
    room["bankerSignal"] = True
    room["timerKind"] = "banker"
    say(room, "Banker signal", "No more bets. Dealer Elias is dealing.")
    deal_round(room)
    if room["phase"] != "betting":
        room["bankerSignal"] = False
        if room["phase"] == "acting":
            room["timerKind"] = "turn"
        elif room["phase"] == "insurance":
            room["timerKind"] = "insurance"
        else:
            room["timerKind"] = ""
    return True


def cards_left(room):
    deal_until = room.get("dealUntil") or len(room.get("feed") or [])
    return max(0, deal_until - room.get("feedIndex", 0))


def draw(room):
    if room["feedIndex"] >= len(room["feed"]):
        return None
    card = room["feed"][room["feedIndex"]]
    room["feedIndex"] += 1
    return card


def commit_feed(room, cards):
    old = room["feed"]
    extended = (
        len(old) > 0
        and len(cards) >= len(old)
        and all(old[i] == cards[i] for i in range(len(old)))
    )
    room["feed"] = list(cards)
    room["feedSig"] = "|".join(cards)
    if not extended:
        room["feedIndex"] = 0
    else:
        room["feedIndex"] = min(room["feedIndex"], len(cards))
    room["pendingFeed"] = None
    apply_limits(room)


def sync_feed(room):
    cards = feed_for_room(room["id"])
    sig = "|".join(cards)
    if sig == room["feedSig"]:
        if cards:
            apply_limits(room)
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
    if room["id"] != "live":
        if not open_machine_shoe(room, needed):
            say(room, "Shoe is at the cut", "The next shoe could not be built. Generate one on e Blackjack.")
            return
    elif cards_left(room) < needed:
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
        room["timerKind"] = "insurance"
        room["bankerSignal"] = False
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
        room["timerKind"] = "insurance"
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
                room["timerKind"] = "turn"
                room["bankerSignal"] = False
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
    room["timerKind"] = "banker"
    room["bankerSignal"] = True
    room["deadline"] = 0
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
    room["timerKind"] = "next"
    room["bankerSignal"] = False
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
    room["timerKind"] = ""
    room["bankerSignal"] = False
    flush_pending_feed(room)
    say(room, "Place your bets", "The banker deals when the timer ends, or when every seated player has a main bet.")


# ------------------------- timeouts + presence -------------------------

def prune_presence(room):
    now = time.time()
    room["players"] = {
        pid: seen for pid, seen in room.get("players", {}).items()
        if now - seen <= PRESENCE_SECS
    }


def presence_counts(room):
    prune_presence(room)
    seated_ids = {
        seat["playerId"]
        for seat in room["seats"]
        if seat["occupied"] and seat.get("playerId")
    }
    online = len(room["players"])
    watching = sum(1 for pid in room["players"] if pid not in seated_ids)
    return online, watching


def tick(room):
    sync_feed(room)
    now = time.time()
    prune_presence(room)
    for _ in range(30):
        phase = room["phase"]
        if phase == "betting" and room["deadline"] and now > room["deadline"]:
            if betting_seats(room):
                banker_deal(room)
                bump(room)
            else:
                clear_bet_timer(room)
            continue
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
            bump(room)
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
        if room["phase"] == "betting" and not betting_seats(room):
            clear_bet_timer(room)


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
            if room.get("bankerSignal"):
                return False, "Banker signal is on. No more bets."
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
            say(room, f"Betting for {seat['name']}", "Main bet at least $25. Wait for the banker signal.")
            if seat["bet"] >= MIN_BET:
                arm_bet_timer(room)
        elif action == "clear":
            clear_bets(seat)
            if not betting_seats(room):
                clear_bet_timer(room)
                say(room, "Place your bets", "The banker waits for at least one main bet.")
        elif action == "rebet":
            if room.get("bankerSignal"):
                return False, "Banker signal is on. No more bets."
            clear_bets(seat)
            wanted = seat["lastBet"] + seat["lastPair"] + seat["lastJackpot"]
            if MIN_BET <= wanted <= seat["bankroll"]:
                seat["bet"] = seat["lastBet"]
                seat["pairBet"] = seat["lastPair"]
                seat["jackpotBet"] = seat["lastJackpot"]
                if seat["bet"] >= MIN_BET:
                    arm_bet_timer(room)
        elif action == "rebuy":
            seat["bankroll"] += BUY_IN
            say(room, f"{seat['name']} rebuys", "Fresh chips added.")
        elif action == "leave":
            room["seats"][index] = empty_seat()
            say(room, "Seat open", "Another player can sit there.")
            if not betting_seats(room):
                clear_bet_timer(room)
        bump(room)
        return True, ""

    if action == "deal":
        return False, "Players do not deal. Place your bet and wait for the banker signal."

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
    online, watching = presence_counts(room)
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
        "timerKind": room.get("timerKind") or "",
        "bankerSignal": bool(room.get("bankerSignal")),
        "online": online,
        "watchers": watching,
        "serverNow": time.time(),
    }


def source_headline(room):
    return "Loaded from machine" if room["loadMode"] == "machine" else "Loaded from live"
