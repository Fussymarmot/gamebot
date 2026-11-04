"""Правила Техасского холдема (no-limit): колода, оценка комбинаций, торги, банки и вскрытие.

Модуль ничего не знает про Telegram: им управляет handlers.py. Состояние меняется только методами Table.

Карта: число 0..51. Достоинство: c % 13 + 2 (2..14, туз = 14), масть: c // 13.
"""
import itertools
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

SUITS = "♠♥♦♣"
FACE = {10: "10", 11: "J", 12: "Q", 13: "K", 14: "A"}

NOM_S = {2: "двойка", 3: "тройка", 4: "четвёрка", 5: "пятёрка", 6: "шестёрка", 7: "семёрка", 8: "восьмёрка",
         9: "девятка", 10: "десятка", 11: "валет", 12: "дама", 13: "король", 14: "туз"}
GEN_S = {2: "двойки", 3: "тройки", 4: "четвёрки", 5: "пятёрки", 6: "шестёрки", 7: "семёрки", 8: "восьмёрки",
         9: "девятки", 10: "десятки", 11: "валета", 12: "дамы", 13: "короля", 14: "туза"}
NOM_P = {2: "двойки", 3: "тройки", 4: "четвёрки", 5: "пятёрки", 6: "шестёрки", 7: "семёрки", 8: "восьмёрки",
         9: "девятки", 10: "десятки", 11: "валеты", 12: "дамы", 13: "короли", 14: "тузы"}
GEN_P = {2: "двоек", 3: "троек", 4: "четвёрок", 5: "пятёрок", 6: "шестёрок", 7: "семёрок", 8: "восьмёрок",
         9: "девяток", 10: "десяток", 11: "валетов", 12: "дам", 13: "королей", 14: "тузов"}

STREET_RU = {"idle": "Ожидание", "preflop": "Префлоп", "flop": "Флоп", "turn": "Тёрн", "river": "Ривер",
             "done": "Раздача завершена"}


def rank(c: int) -> int:
    return c % 13 + 2


def suit(c: int) -> int:
    return c // 13


def card_str(c: int) -> str:
    r = rank(c)
    return FACE.get(r, str(r)) + SUITS[suit(c)]


# ---------- оценка комбинаций ----------

def _score5(cards) -> Tuple[int, ...]:
    """Сила пяти карт: кортеж (категория, тай-брейки...), сравнивается обычным max()."""
    ranks = sorted((rank(c) for c in cards), reverse=True)
    flush = len({suit(c) for c in cards}) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight = 0
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight = uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:  # колесо: A-2-3-4-5
            straight = 5
    counts: Dict[int, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [g[1] for g in groups]
    kick = [g[0] for g in groups]
    if straight and flush:
        return (8, straight)
    if shape == [4, 1]:
        return (7, *kick)
    if shape == [3, 2]:
        return (6, *kick)
    if flush:
        return (5, *ranks)
    if straight:
        return (4, straight)
    if shape == [3, 1, 1]:
        return (3, *kick)
    if shape == [2, 2, 1]:
        return (2, *kick)
    if shape == [2, 1, 1, 1]:
        return (1, *kick)
    return (0, *ranks)


def best_hand(cards) -> Tuple[int, ...]:
    """Лучшая комбинация из 5+ карт."""
    cards = list(cards)
    if len(cards) < 5:
        raise ValueError("нужно минимум 5 карт")
    return max(_score5(combo) for combo in itertools.combinations(cards, 5))


def describe(score: Tuple[int, ...]) -> str:
    cat, tb = score[0], score[1:]
    if cat == 8:
        return "Роял-флеш" if tb[0] == 14 else f"Стрит-флеш до {GEN_S[tb[0]]}"
    if cat == 7:
        return f"Каре: {GEN_P[tb[0]]}"
    if cat == 6:
        return f"Фулл-хаус: {NOM_P[tb[0]]} и {NOM_P[tb[1]]}"
    if cat == 5:
        return f"Флеш до {GEN_S[tb[0]]}"
    if cat == 4:
        return f"Стрит до {GEN_S[tb[0]]}"
    if cat == 3:
        return f"Тройка: {GEN_P[tb[0]]}"
    if cat == 2:
        return f"Две пары: {NOM_P[tb[0]]} и {NOM_P[tb[1]]}"
    if cat == 1:
        return f"Пара {GEN_P[tb[0]]}"
    return f"Старшая карта: {NOM_S[tb[0]]}"


def describe_cards(hole: List[int], board: List[int]) -> str:
    """Подсказка игроку: что у него сейчас на руках (до флопа и после)."""
    if len(hole) < 2:
        return "—"
    if len(board) >= 3:
        return describe(best_hand(list(hole) + list(board)))
    a, b = sorted(hole, key=rank, reverse=True)
    if rank(a) == rank(b):
        return f"Карманная пара {GEN_P[rank(a)]}"
    text = f"Старшая карта: {NOM_S[rank(a)]}"
    if suit(a) == suit(b):
        text += ", одномастные"
    return text


# ---------- стол ----------

@dataclass
class Seat:
    uid: int
    name: str
    stack: int
    hole: List[int] = field(default_factory=list)
    bet: int = 0        # поставлено на текущей улице
    total: int = 0      # поставлено за всю раздачу
    folded: bool = False
    allin: bool = False
    in_hand: bool = False
    leaving: bool = False   # собирается уйти: сбросит карты и покинет стол после раздачи
    out: bool = False       # выбыл (нет фишек) или ушёл
    last: str = ""          # последнее действие на улице


class Table:
    def __init__(self, seats: List[Seat], sb: int, bb: int):
        self.seats = seats
        self.sb, self.bb = sb, bb
        self.button = random.randrange(len(seats))
        self.hand_no = 0
        self.street = "idle"
        self.board: List[int] = []
        self.deck: List[int] = []
        self.cur_bet = 0
        self.min_raise = bb
        self.acted = set()
        self.to_act: Optional[int] = None
        self.cursor = 0
        self.runout = False
        self.result: Optional[dict] = None
        self.busted: List[int] = []
        self.sb_idx = self.bb_idx = -1

    # --- вспомогательное ---

    def seat_of(self, uid: int) -> Optional[Seat]:
        return next((s for s in self.seats if s.uid == uid), None)

    def current(self) -> Optional[Seat]:
        return self.seats[self.to_act] if self.to_act is not None else None

    def pot(self) -> int:
        return sum(s.total for s in self.seats)

    def alive(self) -> List[Seat]:
        return [s for s in self.seats if not s.out]

    def role_of(self, s: Seat) -> str:
        i = self.seats.index(s)
        if not s.in_hand or self.street in ("idle", "done"):
            return ""
        tags = []
        if i == self.button:
            tags.append("D")
        if i == self.sb_idx:
            tags.append("SB")
        if i == self.bb_idx:
            tags.append("BB")
        return "/".join(tags)

    def _next_in_hand(self, idx: int) -> int:
        n = len(self.seats)
        for k in range(1, n + 1):
            i = (idx + k) % n
            if self.seats[i].in_hand:
                return i
        return idx

    def _find_actor(self, cursor: int) -> Optional[int]:
        n = len(self.seats)
        for k in range(n):
            i = (cursor + k) % n
            s = self.seats[i]
            if s.in_hand and not s.folded and not s.allin:
                return i
        return None

    def _put(self, s: Seat, amount: int) -> int:
        amt = max(0, min(amount, s.stack))
        s.stack -= amt
        s.bet += amt
        s.total += amt
        if s.stack == 0:
            s.allin = True
        return amt

    def cleanup(self) -> Tuple[List[Seat], List[Seat]]:
        """Убирает из-за стола обанкротившихся и тех, кто ушёл. Возвращает (выбывшие, ушедшие)."""
        busted, left = [], []
        for s in self.seats:
            if s.out:
                continue
            if s.leaving:
                s.out = True
                left.append(s)
            elif s.stack <= 0:
                s.out = True
                busted.append(s)
                self.busted.append(s.uid)
        return busted, left

    def standings(self) -> List[Seat]:
        """Итоговое место: сначала оставшиеся и ушедшие по стеку, затем выбывшие (кто позже, тот выше)."""
        rest = [s for s in self.seats if s.uid not in self.busted]
        rest.sort(key=lambda s: -s.stack)
        order = {uid: i for i, uid in enumerate(self.busted)}
        out = sorted((s for s in self.seats if s.uid in self.busted), key=lambda s: -order[s.uid])
        return rest + out

    # --- раздача ---

    def start_hand(self) -> bool:
        self.cleanup()
        if len(self.alive()) < 2:
            return False
        self.hand_no += 1
        self.deck = list(range(52))
        random.shuffle(self.deck)
        self.board = []
        self.result = None
        self.runout = False
        self.street = "preflop"
        for s in self.seats:
            s.hole, s.bet, s.total, s.folded, s.allin, s.last = [], 0, 0, False, False, ""
            s.in_hand = not s.out
        n = len(self.seats)
        self.button = self._next_in_hand(self.button)
        order = [i for i in ((self.button + 1 + k) % n for k in range(n)) if self.seats[i].in_hand]
        if len(order) == 2:   # хедз-ап: кнопка = малый блайнд
            self.sb_idx, self.bb_idx = self.button, order[0]
        else:
            self.sb_idx, self.bb_idx = order[0], order[1]
        self._put(self.seats[self.sb_idx], self.sb)
        self._put(self.seats[self.bb_idx], self.bb)
        self.seats[self.sb_idx].last = f"малый блайнд {self.seats[self.sb_idx].bet}"
        self.seats[self.bb_idx].last = f"большой блайнд {self.seats[self.bb_idx].bet}"
        self.cur_bet = max(s.bet for s in self.seats)
        self.min_raise = self.bb
        for s in self.seats:
            if s.in_hand:
                s.hole = [self.deck.pop(), self.deck.pop()]
        self.acted = set()
        self.cursor = (self.bb_idx + 1) % n
        self._progress()
        return True

    def _round_done(self, actors: List[Seat]) -> bool:
        if not actors:
            return True
        if len(actors) == 1:
            return actors[0].bet >= self.cur_bet
        return all(a.uid in self.acted and a.bet == self.cur_bet for a in actors)

    def _progress(self):
        while True:
            live = [s for s in self.seats if s.in_hand and not s.folded]
            if len(live) == 1:
                self._award_fold(live[0])
                return
            actors = [s for s in live if not s.allin]
            if self._round_done(actors):
                if self.street == "river":
                    self._showdown()
                    return
                if len(actors) < 2:   # торговаться больше некому: открываем карты до конца
                    self.runout = True
                    self.to_act = None
                    return
                self._deal_street()
                continue
            idx = self._find_actor(self.cursor)
            seat = self.seats[idx]
            if seat.leaving:          # ушедший игрок автоматически сбрасывает карты
                seat.folded = True
                seat.last = "фолд"
                self.cursor = (idx + 1) % len(self.seats)
                continue
            self.to_act = idx
            return

    def _deal_street(self):
        nxt = {"preflop": "flop", "flop": "turn", "turn": "river"}[self.street]
        for _ in range(3 if nxt == "flop" else 1):
            self.board.append(self.deck.pop())
        self.street = nxt
        for s in self.seats:
            s.bet = 0
            s.last = ""
        self.cur_bet = 0
        self.min_raise = self.bb
        self.acted = set()
        self.cursor = (self.button + 1) % len(self.seats)

    def step_runout(self):
        """Один шаг открытия карт, когда все в олл-ине: следующая улица или вскрытие."""
        if self.street == "river":
            self._showdown()
        else:
            self._deal_street()

    # --- действия игрока ---

    def raise_limits(self, s: Seat) -> Optional[Tuple[int, int]]:
        """(минимальный, максимальный) размер «рейза до», либо None, если рейз невозможен."""
        to_call = self.cur_bet - s.bet
        if s.stack <= to_call:
            return None
        if not any(o is not s and o.in_hand and not o.folded and not o.allin for o in self.seats):
            return None
        hi = s.bet + s.stack
        return min(self.cur_bet + self.min_raise, hi), hi

    def _turn(self, uid: int) -> Seat:
        s = self.current()
        if self.street in ("idle", "done") or self.runout or s is None or s.uid != uid:
            raise ValueError("сейчас не ход этого игрока")
        return s

    def _after(self, s: Seat):
        self.cursor = (self.seats.index(s) + 1) % len(self.seats)
        self._progress()

    def fold(self, uid: int):
        s = self._turn(uid)
        s.folded = True
        s.last = "фолд"
        self._after(s)

    def check_call(self, uid: int):
        s = self._turn(uid)
        need = self.cur_bet - s.bet
        if need <= 0:
            s.last = "чек"
        else:
            paid = self._put(s, need)
            s.last = f"колл {paid}" + (" (олл-ин)" if s.allin else "")
        self.acted.add(s.uid)
        self._after(s)

    def raise_to(self, uid: int, to: int):
        s = self._turn(uid)
        lim = self.raise_limits(s)
        if not lim:
            raise ValueError("рейз невозможен")
        lo, hi = lim
        if to >= hi:
            to = hi
        elif to < lo:
            raise ValueError("рейз меньше минимального")
        self._put(s, to - s.bet)
        inc = to - self.cur_bet
        verb = "ставка" if self.cur_bet == 0 else "рейз до"
        if inc >= self.min_raise:
            self.min_raise = inc
        self.cur_bet = to
        self.acted = {s.uid}
        s.last = f"{verb} {to}" + (" (олл-ин)" if s.allin else "")
        self._after(s)

    def all_in(self, uid: int):
        s = self._turn(uid)
        if self.raise_limits(s) is None:
            self.check_call(uid)
        else:
            self.raise_to(uid, s.bet + s.stack)

    # --- итоги ---

    def _award_fold(self, winner: Seat):
        pot = self.pot()
        winner.stack += pot
        self.result = {"kind": "fold", "pots": [{"amount": pot, "winners": [winner.uid], "label": "Банк"}],
                       "shown": [], "won": {winner.uid: pot}}
        self.street = "done"
        self.runout = False
        self.to_act = None

    def _showdown(self):
        n = len(self.seats)
        live = [s for s in self.seats if s.in_hand and not s.folded]
        scores = {s.uid: best_hand(s.hole + self.board) for s in live}
        contrib = {s.uid: s.total for s in self.seats if s.in_hand}
        pos = {s.uid: (i - self.button - 1) % n for i, s in enumerate(self.seats)}
        won: Dict[int, int] = {}
        pots, prev = [], 0
        for lv in sorted({t for t in contrib.values() if t > 0}):
            amount = sum(min(t, lv) - min(t, prev) for t in contrib.values())
            contributors = sum(1 for t in contrib.values() if t > prev)
            prev = lv
            elig = [s for s in live if s.total >= lv] or live
            best = max(scores[s.uid] for s in elig)
            winners = sorted((s.uid for s in elig if scores[s.uid] == best), key=lambda u: pos[u])
            share, rem = divmod(amount, len(winners))
            for i, u in enumerate(winners):
                got = share + (1 if i < rem else 0)
                self.seat_of(u).stack += got
                won[u] = won.get(u, 0) + got
            if contributors == 1 and len(elig) == 1:
                label = "Возврат неотвеченной ставки"
            else:
                label = "Основной банк" if not pots else f"Доп. банк {len(pots)}"
            pots.append({"amount": amount, "winners": winners, "label": label})
        shown = [(s.uid, list(s.hole), describe(scores[s.uid]))
                 for s in sorted(live, key=lambda s: pos[s.uid])]
        self.result = {"kind": "showdown", "pots": pots, "shown": shown, "won": won}
        self.street = "done"
        self.runout = False
        self.to_act = None
