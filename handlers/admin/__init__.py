"""Админ-панель (только личка с админом). Каждый раздел лежит в своём *_panel.py."""
from handlers.admin import (
    broadcast_panel, cleanup_panel, features_panel, games_panel, giveaway_panel, mods_panel, shop_panel,
    system_panel)
from handlers.admin.common import panel, router
