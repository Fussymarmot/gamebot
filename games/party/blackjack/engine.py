"""Правила блэкджека между игроками (без дилера).

Каждый получает две карты и сам решает: взять ещё или остановиться. Перебор (больше 21) означает автоматическое
поражение. Побеждает тот, у кого сумма ближе всех к 21; при равенстве выигрыш делится.

Модуль ничего не знает про Telegram: им управляет handlers.py.
Карта: число 0..51. Достоинство: c % 13 (0 = туз, 10..12 = валет, дама, король), масть: c // 13.
"""
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SUITS = "♠♥♦♣"
FACES = {0: "A", 10: "J", 11: "Q", 12: "K"}
TARGET = 21

# Состояния игрока
PLAY, STAND, BUST, LEFT = "play", "stand", "bust", "left"


def card_str(c: int) -> str:
    r = c % 13
    return FACES.get(r, str(r + 1)) + SUITS[c // 13]


def card_value(c: int) -> int:
    r = c % 13
    if r == 0:
        return 1          # туз считается за 1, при возможности добавляется ещё 10
    return min(r + 1, 10)


def total(cards: List[int]) -> int:
    """Лучшая сумма: туз считается за 11, пока это не приводит к перебору."""
    s = sum(card_value(c) for c in cards)
    if any(c % 13 == 0 for c in cards) and s + 10 <= TARGET:
        s += 10
    return s


@dataclass
class Player:
    uid: int
    name: str
    cards: List[int] = field(default_factory=list)
    state: str = PLAY
    idle: float = 0.0     # сколько секунд игрок не делал ход

    @property
    def score(self) -> int:
        return total(self.cards)

    @property
    def in_race(self) -> bool:
        """Ещё может победить: не вышел и не перебрал."""
        return self.state in (PLAY, STAND)


class Game:
    def __init__(self, players: Dict[int, str]):
        self.players: List[Player] = [Player(uid, name) for uid, name in players.items()]
        random.shuffle(self.players)
        self.deck: List[int] = []
        self._shuffle()
        for p in self.players:
            p.cards = [self.draw(), self.draw()]
            if p.score == TARGET:     # с двух карт набрал 21: больше брать нечего
                p.state = STAND

    def _shuffle(self):
        self.deck = list(range(52))
        random.shuffle(self.deck)

    def draw(self) -> int:
        if not self.deck:             # колода закончилась: берём новую
            self._shuffle()
        return self.deck.pop()

    def get(self, uid: int) -> Optional[Player]:
        return next((p for p in self.players if p.uid == uid), None)

    def active(self) -> List[Player]:
        return [p for p in self.players if p.state == PLAY]

    def contenders(self) -> List[Player]:
        return [p for p in self.players if p.in_race]

    def over(self) -> bool:
        """Игра закончена: никто больше не ходит, либо остался один претендент на победу."""
        return not self.active() or len(self.contenders()) <= 1

    # --- действия ---

    def hit(self, uid: int) -> Player:
        p = self.get(uid)
        if p is None or p.state != PLAY:
            raise ValueError("сейчас нельзя брать карту")
        p.cards.append(self.draw())
        p.idle = 0
        if p.score > TARGET:
            p.state = BUST
        elif p.score == TARGET:
            p.state = STAND
        return p

    def stand(self, uid: int) -> Player:
        p = self.get(uid)
        if p is None or p.state != PLAY:
            raise ValueError("сейчас нельзя остановиться")
        p.state = STAND
        p.idle = 0
        return p

    def leave(self, uid: int) -> Player:
        p = self.get(uid)
        if p is None or p.state in (LEFT, BUST):
            raise ValueError("уже нельзя выйти")
        p.state = LEFT
        return p

    # --- итоги ---

    def winners(self) -> List[Player]:
        """Ближе всех к 21 среди тех, кто не вышел и не перебрал. Если таких нет, победителей нет."""
        alive = self.contenders()
        if not alive:
            return []
        best = max(p.score for p in alive)
        return [p for p in alive if p.score == best]
