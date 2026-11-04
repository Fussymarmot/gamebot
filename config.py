"""Конфигурация бота: значения читаются из .env в корне проекта."""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
GROUP_ID = int(os.getenv("GROUP_ID", 0))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "claude-haiku-5-5")
AI_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").strip().rstrip("/")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не указан в .env файле")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID не указан в .env файле")
if not GROUP_ID:
    raise ValueError("GROUP_ID не указан в .env файле")
