import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from games.party.mafia.roles import build_roles, JOURNALIST_SEES, NAME, TEAM

MAFIA_KILLERS = ("don", "mafia")


@dataclass
class Player:
    uid: int
    name: str
    role: str = "citizen"
    alive: bool = True
    lives: int = 1
    day_blocked: bool = False
    shield: bool = False
    heal_used: bool = False
    shield_used: bool = False
    attackers: List[int] = field(default_factory=list)
    marked: List[int] = field(default_factory=list)
    kills: int = 0
    disguise: Optional[int] = None
    transform_to: Optional[str] = None
    snitch_hit: bool = False
    start_role: str = "citizen"


@dataclass
class NightResult:
    pub: List[str]
    priv: Dict[int, List[str]]
    deaths: List[Tuple[Player, str]]


@dataclass
class VoteResult:
    lynched: Optional[int]
    text: str
    kamikaze: Optional[int] = None


class MafiaGame:
    def __init__(self, gid: int, chat_id: int, creator_id: int):
        self.gid = gid
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.players: Dict[int, Player] = {}
        self.phase = "lobby"
        self.day = 0
        self.night: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
        self.votes: Dict[int, Optional[int]] = {}
        self.suicide_uid: Optional[int] = None
        self.arsonist_uid: Optional[int] = None

    def add_player(self, uid: int, name: str) -> bool:
        if self.phase != "lobby" or uid in self.players:
            return False
        self.players[uid] = Player(uid, name)
        return True

    def remove_player(self, uid: int) -> bool:
        if self.phase != "lobby" or uid not in self.players:
            return False
        del self.players[uid]
        return True

    def start(self, disabled=(), min_players: int = 5) -> bool:
        if self.phase != "lobby" or len(self.players) < min_players:
            return False
        ids = list(self.players)
        random.shuffle(ids)
        roles = build_roles(len(ids), disabled)
        random.shuffle(roles)
        for uid, role in zip(ids, roles):
            p = self.players[uid]
            p.role = p.start_role = role
            p.lives = 2 if role == "lucky" else 1
        self.phase = "night"
        self.day = 0
        return True

    def reset_to_lobby(self):
        self.phase = "lobby"
        for p in self.players.values():
            p.role = p.start_role = "citizen"
            p.alive = True
            p.lives = 1

    def alive(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def team(self, uid: int) -> str:
        return TEAM[self.players[uid].role]

    def mafia_team_uids(self, alive_only=True) -> List[int]:
        return [p.uid for p in self.players.values()
                if TEAM[p.role] == "mafia" and (p.alive or not alive_only)]

    def group_killers(self) -> List[int]:
        return [p.uid for p in self.alive() if p.role in MAFIA_KILLERS]

    def begin_night(self):
        self.day += 1
        self.phase = "night"
        self.night.clear()
        for p in self.players.values():
            if p.alive and p.transform_to:
                p.role = p.transform_to
                p.transform_to = None

    def night_options(self, uid: int) -> Optional[dict]:
        p = self.players.get(uid)
        if not p or not p.alive or self.phase != "night":
            return None
        alive = self.alive()
        others = [q for q in alive if q.uid != uid]
        r = p.role
        if r in ("don", "mafia", "killer"):
            t = [q for q in alive if TEAM[q.role] != "mafia"]
        elif r == "lawyer":
            t = others if p.shield_used else alive
        elif r == "doctor":
            t = others if p.heal_used else alive
        elif r in ("journalist", "lover", "hobo", "maniac", "swindler", "snitch", "commissar"):
            t = others
        elif r == "arsonist":
            t = alive
        elif r == "mage":
            t = [self.players[a] for a in p.attackers if self.players[a].alive]
            if not t:
                return None
        else:
            return None
        return {
            "role": r,
            "mode": "commissar" if r == "commissar" else "target",
            "targets": [(q.uid, q.name) for q in t],
            "can_shoot": r == "commissar" and self.day >= 2,
        }

    def submit_night(self, uid: int, target: Optional[int], variant: Optional[str] = None) -> bool:
        opts = self.night_options(uid)
        if not opts:
            return False
        if target is None:
            self.night[uid] = (None, None)
            return True
        if target not in [t for t, _ in opts["targets"]]:
            return False
        if opts["mode"] == "commissar":
            if variant not in ("check", "shoot"):
                return False
            if variant == "shoot" and not opts["can_shoot"]:
                return False
        self.night[uid] = (target, variant)
        return True

    def pending_night(self) -> List[int]:
        return [p.uid for p in self.alive()
                if self.night_options(p.uid) and p.uid not in self.night]

    def all_night_done(self) -> bool:
        return not self.pending_night()

    def resolve_night(self) -> NightResult:
        P = self.players
        pub: List[str] = []
        priv: Dict[int, List[str]] = defaultdict(list)
        deaths: List[Tuple[Player, str]] = []
        acts = {u: a for u, a in self.night.items() if P[u].alive and a[0] is not None}

        blocked = set()
        for u, (t, v) in acts.items():
            if P[u].role == "lover":
                if P[t].role == "commissar":
                    priv[u].append(f"Ты провела ночь у {P[t].name}.")
                else:
                    blocked.add(t)
                    P[t].day_blocked = True
                    priv[t].append("К тебе приходила любовница: этой ночью ты не мог действовать, "
                                   "а завтра не сможешь голосовать.")
                    priv[u].append(f"Ты провела ночь у {P[t].name}.")
        live = {u: a for u, a in acts.items() if u not in blocked}

        visitors: Dict[int, List[int]] = defaultdict(list)
        for u, (t, v) in live.items():
            if P[u].role not in MAFIA_KILLERS:
                visitors[t].append(u)

        members = [u for u in live if P[u].role in MAFIA_KILLERS]
        shooter = gt = None
        if members:
            shooter = next((u for u in members if P[u].role == "don"), members[0])
            if P[shooter].role == "don":
                gt = live[shooter][0]
            else:
                gt = Counter(live[u][0] for u in members).most_common(1)[0][0]
            visitors[gt].append(shooter)

        healed = {}
        disguised = set()
        for u, (t, v) in live.items():
            r = P[u].role
            if r == "doctor":
                healed[t] = u
                if t == u:
                    P[u].heal_used = True
            elif r == "lawyer":
                P[t].shield = True
                disguised.add(t)
                if t == u:
                    P[u].shield_used = True

        attacks = []
        if gt is not None:
            attacks.append((shooter, gt, "mafia"))
        for u, (t, v) in live.items():
            r = P[u].role
            if r == "killer":
                if P[t].role == "commissar":
                    attacks.append((u, u, "backfire"))
                else:
                    attacks.append((u, t, "killer"))
            elif r == "maniac":
                attacks.append((u, t, "maniac"))
            elif r == "commissar" and v == "shoot":
                attacks.append((u, t, "shoot"))
            elif r == "mage":
                attacks.append((u, t, "mage"))

        def die(uid, cause):
            if P[uid].alive:
                P[uid].alive = False
                deaths.append((P[uid], cause))

        for a, v, kind in attacks:
            V, A = P[v], P[a]
            if not V.alive:
                continue
            if kind == "backfire":
                priv[a].append("Ты попытался убить комиссара и погиб сам.")
                die(a, kind)
                continue
            if V.role == "killer":
                continue
            if V.role == "mage" and kind != "mage" and A.role in ("don", "maniac", "commissar"):
                V.attackers.append(a)
                priv[v].append(f"Этой ночью тебя пытался убить {A.name}, но у него не вышло. "
                               "Следующей ночью можешь наказать его.")
                continue
            if v in healed and kind != "killer":
                priv[v].append("Этой ночью на тебя напали, но тебя спас доктор.")
                priv[healed[v]].append(f"Ты спас {V.name}: на него напали этой ночью.")
                continue
            if V.lives > 1:
                V.lives -= 1
                priv[v].append("Этой ночью тебя пытались убить. Ты потерял одну жизнь, но жив.")
                continue
            if V.role == "werewolf" and kind in ("mafia", "shoot"):
                V.transform_to = "mafia" if kind == "mafia" else "sergeant"
                priv[v].append("Тебя пытались убить, но ты оборотень! "
                               f"Со следующей ночи ты {NAME[V.transform_to]}.")
                continue
            die(v, kind)
            if V.role == "kamikaze" and A.alive and A.role != "killer":
                priv[a].append("Ты напал на камикадзе и погиб вместе с ним.")
                die(a, "kamikaze")

        for u, (t, v) in live.items():
            A = P[u]
            if A.role != "arsonist" or not A.alive:
                continue
            if t == u:
                victims = [m for m in A.marked if P[m].alive]
                for m in victims:
                    die(m, "fire")
                A.kills += len(victims)
                die(u, "fire")
                if A.kills >= 3:
                    self.arsonist_uid = u
            else:
                if t not in A.marked:
                    A.marked.append(t)
                priv[u].append(f"Ты облил {P[t].name}. Всего облито: {len(A.marked)}.")

        checked = None
        for u, (t, v) in live.items():
            r = P[u].role
            if r == "hobo":
                vis = [P[x].name for x in visitors.get(t, []) if x != u]
                priv[u].append(f"Ты зашёл к {P[t].name}. " +
                               (f"Этой ночью к нему приходили: {', '.join(vis)}." if vis
                                else "К нему никто не приходил."))
            elif r == "journalist":
                if P[t].role in JOURNALIST_SEES:
                    priv[u].append(f"{P[t].name}: {NAME[P[t].role]}.")
                else:
                    priv[u].append(f"{P[t].name}: ничего подозрительного.")
            elif r == "commissar" and v == "check":
                is_m = TEAM[P[t].role] == "mafia" and t not in disguised
                priv[u].append(f"{P[t].name}: {'МАФИЯ' if is_m else 'мирный житель'}.")
                checked = t
            elif r == "swindler":
                P[u].disguise = t
                priv[u].append(f"Завтра на голосовании ты можешь голосовать и от имени {P[t].name}.")
        if checked is not None:
            for u, (t, v) in live.items():
                if P[u].role == "snitch" and t == checked and P[u].alive:
                    P[u].snitch_hit = True
                    pub.append(f"Стукач раскрыл: {P[checked].name} — {NAME[P[checked].role]}.")

        if not any(p.alive and p.role == "commissar" for p in P.values()):
            for p in P.values():
                if p.alive and p.role == "sergeant":
                    p.role = "commissar"
                    priv[p.uid].append("Комиссар погиб. Теперь ты Комиссар Каттани!")
                    break

        self.night.clear()
        self.phase = "day"
        return NightResult(pub, dict(priv), deaths)

    def begin_vote(self):
        self.phase = "vote"
        self.votes.clear()

    def voters(self) -> List[int]:
        return [p.uid for p in self.alive() if not p.day_blocked]

    def submit_vote(self, uid: int, target: Optional[int]) -> bool:
        if self.phase != "vote" or uid not in self.voters():
            return False
        if target is not None and (target not in self.players or not self.players[target].alive):
            return False
        self.votes[uid] = target
        return True

    def all_voted(self) -> bool:
        return all(u in self.votes for u in self.voters())

    def _weight(self, p: Player) -> int:
        return 2 if p.role == "mayor" else 1

    def vote_tally(self) -> Counter:
        counts: Counter = Counter()
        for voter, t in self.votes.items():
            if t is None:
                continue
            p = self.players[voter]
            w = self._weight(p)
            if p.role == "swindler" and p.disguise in self.players:
                w += self._weight(self.players[p.disguise])
            counts[t] += w
        return counts

    def resolve_vote(self) -> VoteResult:
        counts = self.vote_tally()
        res = VoteResult(None, "Голоса разделились, никого не линчевали.")
        if counts:
            top = max(counts.values())
            cands = [t for t, c in counts.items() if c == top]
            if len(cands) == 1:
                t = cands[0]
                T = self.players[t]
                if T.shield:
                    res = VoteResult(None, f"{T.name} получил больше всего голосов, "
                                           "но его защитил адвокат. Никого не линчевали.")
                else:
                    T.alive = False
                    if T.role == "suicide":
                        self.suicide_uid = t
                    res = VoteResult(t, f"Горожане линчевали {T.name} — {NAME[T.role]}.",
                                     kamikaze=t if T.role == "kamikaze" else None)
        else:
            res = VoteResult(None, "Никто не проголосовал, никого не линчевали.")
        for p in self.players.values():
            p.shield = False
            p.day_blocked = False
            p.disguise = None
        self.votes.clear()
        self.phase = "day"
        return res

    def kamikaze_take(self, target: Optional[int]) -> Optional[Player]:
        if target is None or target not in self.players or not self.players[target].alive:
            return None
        self.players[target].alive = False
        return self.players[target]

    def check_win(self) -> Optional[str]:
        if self.suicide_uid is not None:
            return "suicide"
        if self.arsonist_uid is not None:
            return "arsonist"
        alive = self.alive()
        if not alive:
            return "nobody"
        maf = [p for p in alive if TEAM[p.role] == "mafia"]
        man = [p for p in alive if p.role == "maniac"]
        ars = [p for p in alive if p.role == "arsonist"]
        if not maf and not man and not ars:
            return "town"
        if man and not maf and len(alive) <= 2:
            return "maniac"
        if maf and len(maf) >= len(alive) - len(maf):
            return "mafia"
        return None

    def winners(self, key: str) -> List[Player]:
        P = list(self.players.values())
        if key == "town":
            main = [p for p in P if TEAM[p.role] == "town" or (p.start_role in
                    ("commissar", "sergeant", "mayor", "doctor", "lover", "hobo", "citizen", "lucky", "kamikaze"))]
        elif key == "mafia":
            main = [p for p in P if TEAM[p.role] == "mafia"]
        elif key == "suicide":
            main = [self.players[self.suicide_uid]]
        elif key == "arsonist":
            main = [self.players[self.arsonist_uid]]
        elif key == "maniac":
            main = [p for p in P if p.role == "maniac" and p.alive]
        else:
            main = []
        seen = {p.uid for p in main}
        extra = [p for p in P if p.uid not in seen and p.alive and (
            p.role in ("mage", "swindler", "werewolf") or (p.role == "snitch" and p.snitch_hit))]
        return main + (extra if key not in ("suicide", "arsonist") else [])
