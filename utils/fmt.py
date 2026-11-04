import re
import html

def b(text: str) -> str:
    return f"<b>{esc(text)}</b>"

def i(text: str) -> str:
    return f"<i>{esc(text)}</i>"

def code(text: str) -> str:
    return f"<code>{esc(text)}</code>"

def pre(text: str) -> str:
    return f"<pre>{esc(text)}</pre>"

def quote(text: str) -> str:
    return f"<blockquote>{esc(text)}</blockquote>"

def esc(text: str) -> str:
    return html.escape(str(text))


_TAGS = re.compile(r"<[^>]+>")


def rich(text) -> str:
    text = str(text or "")
    if "<tg-emoji" in text:
        return text
    return esc(text)


def plain(text) -> str:
    return html.unescape(_TAGS.sub("", str(text or "")))


def admin_text(message) -> str:
    raw = (message.text or "").strip()
    entities = message.entities or []
    if any(en.type == "custom_emoji" for en in entities):
        return (message.html_text or "").strip()
    return raw
