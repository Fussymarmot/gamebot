import json
import os
import random

from config import BASE_DIR

DATA_DIR = os.path.join(BASE_DIR, "data")

# {имя файла: (mtime, данные)}. Файл перечитывается только если его изменили на диске.
_cache: dict = {}


def load(name: str, default):
    path = os.path.join(DATA_DIR, name)
    try:
        mtime = os.stat(path).st_mtime
        hit = _cache.get(name)
        if hit and hit[0] == mtime:
            return hit[1] or default
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _cache[name] = (mtime, data)
        return data or default
    except Exception:
        return default


def assoc_words():
    return load("assoc_words.json", ["космос", "море", "лето"])


def wordseek_words():
    return load("wordseek_words.json", ["книга", "лампа", "озеро"])


def pick(items, n: int):
    items = list(items)
    random.shuffle(items)
    return items[:n]
