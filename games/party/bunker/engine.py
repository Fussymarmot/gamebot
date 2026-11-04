import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

DAYS = 30
TAKE_LIMIT = 3
OFFER_SIZE = 6
START_HP = 10.0
START_MORALE = 6.0
FOOD_PER = 0.185
WATER_PER = 0.15
P_NEED_OK = 0.9
P_NEED_MISSING = 0.3
REVEAL_ORDER = ["prof", "hobby", "bag", "fact"]
REVEAL_LABEL = {"prof": "Профессия", "hobby": "Хобби", "bag": "Багаж", "fact": "Факт"}


@dataclass
class BPlayer:
    uid: int
    name: str
    prof: dict
    hobby: dict
    bag: dict
    fact: dict
    items: List[int] = field(default_factory=list)
    alive: bool = True
    revealed: Set[str] = field(default_factory=set)

    def skills(self) -> Set[str]:
        return set(self.prof["skills"]) | set(self.hobby["skills"]) | set(self.fact["skills"])


class Bunker:
    def __init__(self, names: Dict[int, str], data: dict, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self.data = data
        self.items = data["items"]
        self.players: Dict[int, BPlayer] = {}
        decks = {k: self._deck(data[k], len(names)) for k in ("professions", "hobbies", "baggage", "facts")}
        for uid, name in names.items():
            self.players[uid] = BPlayer(uid, name, decks["professions"].pop(), decks["hobbies"].pop(),
                                        decks["baggage"].pop(), decks["facts"].pop())
        self.offers: Dict[int, List[int]] = {}
        self.food = self.water = 0.0
        self.morale = START_MORALE
        self.hp = START_HP
        self.day = 0
        self.kits: Counter = Counter()
        self.seen: Set[int] = set()
        self.log: List[str] = []

    def _deck(self, cards: list, need: int) -> list:
        deck = []
        while len(deck) < need:
            part = list(cards)
            self.rng.shuffle(part)
            deck += part
        return deck

    def alive(self) -> List[BPlayer]:
        return [p for p in self.players.values() if p.alive]

    def deal_offers(self) -> Dict[int, List[int]]:
        deck: List[int] = []
        for uid in self.players:
            if len(deck) < OFFER_SIZE:
                part = list(range(len(self.items)))
                self.rng.shuffle(part)
                deck += part
            hand, i = [], 0
            while len(hand) < OFFER_SIZE and i < len(deck):
                if deck[i] not in hand:
                    hand.append(deck.pop(i))
                else:
                    i += 1
            self.offers[uid] = hand
        return self.offers

    def toggle(self, uid: int, idx: int) -> Tuple[bool, str]:
        p = self.players[uid]
        if idx not in self.offers.get(uid, []):
            return False, "Этого предмета нет в твоём доме."
        if idx in p.items:
            p.items.remove(idx)
            return True, "Положил обратно."
        if len(p.items) >= TAKE_LIMIT:
            return False, f"Больше {TAKE_LIMIT} предметов не унести."
        p.items.append(idx)
        return True, "Взял!"

    def finish_scavenge(self):
        for p in self.players.values():
            for idx in p.items:
                it = self.items[idx]
                if it["cat"] == "food":
                    self.food += it["v"]
                elif it["cat"] == "water":
                    self.water += it["v"]
                self.kits[it["cat"]] += 1
            if p.bag["cat"] in ("food", "water"):
                if p.bag["cat"] == "food":
                    self.food += p.bag["v"] * 2
                else:
                    self.water += p.bag["v"] * 2
            self.kits[p.bag["cat"]] += 1

    def skills(self) -> Set[str]:
        out: Set[str] = set()
        for p in self.alive():
            out |= p.skills()
        return out

    def has(self, need: Optional[str]) -> bool:
        if not need:
            return True
        kind, name = need.split(":")
        return name in self.skills() if kind == "s" else self.kits.get(name, 0) > 0

    def probability(self, opt: dict) -> float:
        need = opt["need"]
        if not need:
            return opt["p"]
        return P_NEED_OK if self.has(need) else P_NEED_MISSING

    def next_event(self) -> dict:
        pool = [e for e in self.data["events"] if e["id"] not in self.seen] or self.data["events"]
        ev = self.rng.choice(pool)
        self.seen.add(ev["id"])
        return ev

    def apply(self, effects: dict):
        self.food = max(0.0, self.food + effects.get("f", 0))
        self.water = max(0.0, self.water + effects.get("w", 0))
        self.morale = min(10.0, max(0.0, self.morale + effects.get("m", 0)))
        self.hp = min(START_HP, self.hp + effects.get("h", 0))

    def resolve(self, ev: dict, idx: int) -> Tuple[bool, str]:
        opt = ev["options"][idx]
        ok = self.rng.random() < self.probability(opt)
        self.apply(opt["ok"] if ok else opt["bad"])
        return ok, opt["ok_text"] if ok else opt["bad_text"]

    def supply_days(self) -> float:
        n = max(1, len(self.alive()))
        return min(self.food / (n * FOOD_PER), self.water / (n * WATER_PER))

    def end_day(self) -> List[str]:
        n = len(self.alive())
        notes = []
        self.food -= n * FOOD_PER
        self.water -= n * WATER_PER
        if self.food < 0:
            self.food = 0.0
            self.hp -= 1
            notes.append("Еда закончилась, все голодают.")
        if self.water < 0:
            self.water = 0.0
            self.hp -= 2
            notes.append("Вода закончилась, все мучаются от жажды.")
        self.morale = min(10.0, max(0.0, self.morale - 0.3 + 0.1 * min(self.kits.get("mood", 0), 6)))
        if self.morale <= 0:
            self.hp -= 1
            notes.append("Настроение на нуле, начались ссоры.")
        if self.food > 0 and self.water > 0 and self.morale >= 5:
            self.hp = min(START_HP, self.hp + 0.3 + 0.05 * min(self.kits.get("health", 0), 8))
        return notes

    def reveal(self) -> Optional[Tuple[BPlayer, str, str]]:
        cands = [(p, k) for p in self.alive() for k in REVEAL_ORDER if k not in p.revealed]
        if not cands:
            return None
        p, k = self.rng.choice(cands)
        p.revealed.add(k)
        return p, REVEAL_LABEL[k], getattr(p, k)["name"]

    def exclude(self, uid: int):
        self.players[uid].alive = False

    def over(self) -> Optional[str]:
        if self.hp <= 0:
            return "lose"
        if not self.alive():
            return "lose"
        if self.day >= DAYS:
            return "win"
        return None
