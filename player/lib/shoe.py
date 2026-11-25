"""House shoe for e Blackjack tables 2–6.

One ordered 8-deck pack is washed, cut at 75%, and marked with a burn
and a stop. The same profile and seed rebuild the same shoe. A long
face, rank, or suit run is rejected and rebuilt.
"""

import random
import secrets
import time

SHOE_SIZE = 416
FACE = {"10", "J", "Q", "K", "A"}
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]
TABLES = (2, 3, 4, 5, 6)
MAX_FACE_RUN = 12
MAX_RANK_RUN = 6
MAX_SUIT_RUN = 8
MAX_TRIES = 8

PROFILES = {
    "standard": {"cut_lo": 0.55, "cut_hi": 0.70, "csm": False},
    "deep": {"cut_lo": 0.70, "cut_hi": 0.80, "csm": False},
    "csm": {"cut_lo": 0.60, "cut_hi": 0.75, "csm": True},
}


def normalize_profile(name):
    return name if name in PROFILES else "standard"


def fresh_seed():
    return f"deal-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def rank_of(code):
    return "10" if code.startswith("10") else code[0]


def suit_of(code):
    return code[-1]


def ordered_pack():
    cards = []
    for _deck in range(8):
        for rank in RANKS:
            for suit in SUITS:
                cards.append(rank + suit)
    return cards


def legal_shoe(cards):
    if len(cards) != SHOE_SIZE:
        return False
    counts = {}
    for code in cards:
        counts[code] = counts.get(code, 0) + 1
    return len(counts) == 52 and all(count == 8 for count in counts.values())


def longest_face_run(cards):
    best = current = 0
    for code in cards:
        if rank_of(code) in FACE:
            current += 1
            if current > best:
                best = current
        else:
            current = 0
    return best


def longest_same(cards, key):
    best = current = 0
    previous = None
    for code in cards:
        value = key(code)
        if value == previous:
            current += 1
        else:
            previous = value
            current = 1
        if current > best:
            best = current
    return best


def passes_audit(cards):
    if not legal_shoe(cards):
        return False
    if longest_face_run(cards) > MAX_FACE_RUN:
        return False
    if longest_same(cards, rank_of) > MAX_RANK_RUN:
        return False
    if longest_same(cards, suit_of) > MAX_SUIT_RUN:
        return False
    return True


def wash(cards, rng, csm):
    deck = cards[:]
    for index in range(len(deck) - 1, 0, -1):
        swap = rng.randrange(index + 1)
        deck[index], deck[swap] = deck[swap], deck[index]
    if csm:
        moves = len(deck) // 8
        for _move in range(moves):
            src = rng.randrange(len(deck))
            card = deck.pop(src)
            dest = rng.randrange(len(deck) + 1)
            deck.insert(dest, card)
    return deck


def cut_index(rng, profile):
    """Fixed cut at 75% of the shoe, card 312. The same place every shoe."""
    del rng, profile
    return int(SHOE_SIZE * 0.75)


def assemble(profile_name, seed):
    profile = PROFILES[normalize_profile(profile_name)]
    rng = random.Random(seed)
    cards = wash(ordered_pack(), rng, profile["csm"])
    stop = cut_index(rng, profile)
    return cards, stop


def build_shoe(table_number, profile_name, seed):
    """Wash one table. An empty seed mints a new one. Retries keep the same request seed."""
    profile = normalize_profile(profile_name)
    base = str(seed or "").strip() or fresh_seed()
    request_seed = f"{base}:table{int(table_number)}"
    last = None
    for attempt in range(MAX_TRIES):
        use = request_seed if attempt == 0 else f"{request_seed}-retry-{attempt}"
        cards, stop = assemble(profile, use)
        face = longest_face_run(cards)
        last = (cards, stop, face, use)
        if passes_audit(cards):
            break
    cards, stop, face, use = last
    if not legal_shoe(cards):
        raise RuntimeError("House shoe is not 8 of every card")
    return {
        "cards": cards,
        "meta": {
            "profile": profile,
            "seed": use,
            "cutIndex": stop,
            "burn": 1,
            "dealUntil": stop,
            "longestFaceRun": face,
        },
    }


def build_tables(profile_name, seed, tables):
    built = {}
    for number in tables:
        built[int(number)] = build_shoe(number, profile_name, seed)
    return built
