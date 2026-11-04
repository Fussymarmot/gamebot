import json
import os
import random

from config import BASE_DIR

DATA_DIR = os.path.join(BASE_DIR, "data")


def load(name: str, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            data = json.load(f)
            return data or default
    except Exception:
        return default


def who_most_questions():
    return load("who_most.json", [["Кто скорее всего забудет, куда положил телефон?", "Главный забывашка"]])


def assoc_words():
    return load("assoc_words.json", ["космос", "море", "лето"])


def wordseek_words():
    return load("wordseek_words.json", ["книга", "лампа", "озеро"])


def pick(items, n: int):
    items = list(items)
    random.shuffle(items)
    return items[:n]
