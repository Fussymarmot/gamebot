"""Общий логгер бота.

Уровень и формат настраиваются в main.py. В остальных модулях достаточно:
    from utils.logs import logger
    ...
    except Exception:
        logger.exception("что не удалось сделать")
"""
import logging

logger = logging.getLogger("gamebot")
