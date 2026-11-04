import random
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

ROLE_NAME = {"villager": "Мирный житель", "werewolf": "Оборотень",
             "doctor": "Доктор", "detective": "Детектив"}
ROLE_EMOJI = {"villager": "👨", "werewolf": "🐺", "doctor": "👨‍⚕", "detective": "🕵"}
ROLE_DESC = {
    "villager": "Нет особых способностей. Обсуждай, голосуй и найди оборотней.",
    "werewolf": "Ночью вместе с другими оборотнями выбираешь жертву. Днём притворяйся мирным.",
    "doctor": "Каждую ночь выбираешь игрока, которого спасёшь от оборотней. Можно спасти себя.",
    "detective": "Каждую ночь проверяешь игрока и узнаёшь, оборотень он или нет.",
}


@dataclass
class WPlayer:
    uid: int
    name: str
    role: str = "villager"
    alive: bool = True


def wolves_count(n: int) -> int:
    return 1 if n <= 8 else 2 if n <= 12 else 3


class WerewolfGame:
    def __init__(self, players: Dict[int, str], use_doctor: bool = True, use_detective: bool = True):
        self.players: Dict[int, WPlayer] = {u: WPlayer(u, n) for u, n in players.items()}
        self.use_doctor, self.use_detective = use_doctor, use_detective
        self.day = 0
        self.phase = "lobby"
        self.actions: Dict[int, int] = {}
        self.votes: Dict[int, Optional[int]] = {}
        self.candidates: Optional[List[int]] = None

    def assign(self):
        ids = list(self.players)
        random.shuffle(ids)
        roles = ["werewolf"] * wolves_count(len(ids))
        if self.use_doctor:
            roles.append("doctor")
        if self.use_detective:
            roles.append("detective")
        roles += ["villager"] * (len(ids) - len(roles))
        for uid, role in zip(ids, roles):
            self.players[uid].role = role

    def alive(self) -> List[WPlayer]:
        return [p for p in self.players.values() if p.alive]

    def wolves(self) -> List[WPlayer]:
        return [p for p in self.alive() if p.role == "werewolf"]

    def begin_night(self):
        self.day += 1
        self.phase = "night"
        self.actions.clear()

    def options(self, uid: int) -> Optional[List[Tuple[int, str]]]:
        p = self.players.get(uid)
        if not p or not p.alive or self.phase != "night" or p.role == "villager":
            return None
        if p.role == "werewolf":
            return [(q.uid, q.name) for q in self.alive() if q.role != "werewolf"]
        if p.role == "doctor":
            return [(q.uid, q.name) for q in self.alive()]
        return [(q.uid, q.name) for q in self.alive() if q.uid != uid]

    def submit(self, uid: int, target: Optional[int]) -> bool:
        opts = self.options(uid)
        if opts is None:
            return False
        if target is not None and target not in [t for t, _ in opts]:
            return False
        self.actions[uid] = target
        return True

    def pending(self) -> List[int]:
        return [p.uid for p in self.alive() if self.options(p.uid) is not None and p.uid not in self.actions]

    def resolve_night(self) -> dict:
        wolf_votes = Counter(t for u, t in self.actions.items()
                             if t is not None and self.players[u].role == "werewolf")
        victim = None
        if wolf_votes:
            top = max(wolf_votes.values())
            victim = random.choice([t for t, c in wolf_votes.items() if c == top])
        saved = None
        for u, t in self.actions.items():
            if t is not None and self.players[u].role == "doctor" and self.players[u].alive:
                saved = t
        died = None
        if victim is not None and victim != saved:
            self.players[victim].alive = False
            died = victim
        checks = {}
        for u, t in self.actions.items():
            if t is not None and self.players[u].role == "detective":
                checks[u] = (t, self.players[t].role == "werewolf")
        self.phase = "day"
        return {"died": died, "saved": victim is not None and victim == saved, "checks": checks}

    def begin_vote(self, candidates: Optional[List[int]] = None):
        self.phase = "vote"
        self.votes.clear()
        self.candidates = candidates

    def submit_vote(self, voter: int, target: Optional[int]) -> bool:
        p = self.players.get(voter)
        if self.phase != "vote" or not p or not p.alive:
            return False
        if target is not None:
            if target not in self.players or not self.players[target].alive:
                return False
            if self.candidates is not None and target not in self.candidates:
                return False
        self.votes[voter] = target
        return True

    def all_voted(self) -> bool:
        return all(p.uid in self.votes for p in self.alive())

    def tally(self) -> Counter:
        return Counter(t for t in self.votes.values() if t is not None)

    def resolve_vote(self) -> Tuple[Optional[int], List[int]]:
        c = self.tally()
        self.phase = "day"
        if not c:
            return None, []
        top = max(c.values())
        leaders = [t for t, n in c.items() if n == top]
        if len(leaders) == 1:
            self.players[leaders[0]].alive = False
            return leaders[0], []
        return None, leaders

    def check_win(self) -> Optional[str]:
        w = len(self.wolves())
        others = len(self.alive()) - w
        if w == 0:
            return "villagers"
        if w >= others:
            return "wolves"
        return None
