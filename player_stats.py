"""«Игровые моменты» профиля (задача 74).

Заказчик: «профиль — это профиль, карман — это карман». В кармане лежит
имущество, а в профиле хочется видеть след игрока: сколько всего потратил,
какой апгрейд был лучшим, какая ставка была самой крупной, сколько вывел,
сколько открыл сундуков.

Правила модуля:

* **Только правда.** Каждое число либо приходит из существующего журнала
  (`upgrade_log`, `arena_payouts`, `shop_purchases`, `chest_claims`), либо
  прибавляется здесь в момент события. Ничего не «досчитываем на глазок».
* **Статистика не мешает игре.** Нет таблицы, нет сети, база ругается — все
  функции молча пишут в лог и живут дальше: игра важнее счётчиков.
* **Пишем прибавлениями.** Параллельные победы не должны терять друг друга,
  поэтому запись идёт в SQL-функцию `player_stats_add` (атомарный UPDATE).
* **Читаем одним запросом.** Профиль открывается часто: чтение — одна строка
  `player_stats` плюс самые крупные события.

Модуль без aiogram и без FastAPI: его зовут и api.py, и bot.py.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("aura-stats")

STATS_TABLE = "player_stats"
EVENTS_TABLE = "player_events"
STATS_RPC = "player_stats_add"
MIGRATION_FILE = "supabase/migrations/20261202000000_player_stats.sql"

# Счётчики, которые складываются.
SUM_FIELDS = (
    "coins_spent", "spent_upgrade", "spent_shop", "spent_crash", "spent_arena",
    "spent_mines", "spent_dice", "coins_earned", "won_crash", "won_arena",
    "won_mines", "won_dice", "won_upgrade", "won_chests", "won_love",
    "lost_games", "sent_transfer", "sent_transfer_cnt", "got_transfer",
    "chests_opened", "golden_chests", "upgrades_total", "upgrades_won",
    "games_played", "games_won",
)
# Рекорды: остаётся максимум.
MAX_FIELDS = (
    "best_crash_bet", "best_crash_win", "best_arena_bet", "best_arena_win",
    "best_mines_bet", "best_mines_win", "best_dice_bet", "best_dice_win",
    "best_transfer",
)


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def add_stats(supabase_client, user_id, **deltas) -> bool:
    """Прибавить счётчики. Лишние поля отбрасываются, нули не пишем.

    Возвращает True, если запись подтверждена базой. False — не ошибка игры:
    значит просто нет миграции или база занята.
    """
    uid = _int(user_id, None) if user_id is not None else None
    if uid is None:
        return False
    payload = {}
    for key, value in deltas.items():
        if key in SUM_FIELDS or key in MAX_FIELDS:
            amount = _int(value, 0)
            if amount:
                payload[key] = amount
    if not payload:
        return False
    # Основной путь: атомарная функция (одна строка на игрока, без гонок).
    try:
        supabase_client.rpc(STATS_RPC, {"p_user_id": uid, "p_delta": payload}).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — нет миграции или RPC недоступна
        logger.debug("player_stats_add недоступна (%s) — пишем прямым апдейтом", exc)
    # Резерв: функции нет. Читаем строку и прибавляем сами; параллельное событие
    # теоретически может потеряться, но лучше так, чем никак.
    try:
        rows = (supabase_client.table(STATS_TABLE).select("*")
                .eq("user_id", uid).limit(1).execute().data) or []
        row = dict(rows[0]) if rows else {"user_id": uid}
        for key, value in payload.items():
            if key in MAX_FIELDS:
                row[key] = max(_int(row.get(key), 0), _int(value))
            else:
                row[key] = _int(row.get(key), 0) + _int(value)
        row["user_id"] = uid
        supabase_client.table(STATS_TABLE).upsert(row, on_conflict="user_id").execute()
        return True
    except Exception as exc:  # noqa: BLE001 — статистика не должна ронять игру
        logger.warning("Статистика профиля не записана (user_id=%s): %s. "
                       "Примените %s", uid, exc, MIGRATION_FILE)
        return False


def add_event(supabase_client, user_id, kind: str, title: str,
              amount: int = 0, meta: dict | None = None) -> bool:
    """Записать «момент» игрока: апгрейд, перевод, приз.

    Миграция держит на игрока 20 последних событий, поэтому лента не растёт.
    """
    uid = _int(user_id, None) if user_id is not None else None
    if uid is None or kind not in ("upgrade", "transfer", "prize"):
        return False
    payload = {
        "user_id": uid,
        "kind": kind,
        "title": str(title or "")[:180],
        "amount": max(0, _int(amount, 0)),
        "meta": meta or {},
    }
    try:
        supabase_client.table(EVENTS_TABLE).insert(payload).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — нет таблицы: живём дальше
        logger.debug("player_events не записаны (user_id=%s): %s", uid, exc)
        return False


def read_stats(supabase_client, user_id) -> dict:
    """Счётчики игрока. Пустой словарь по умолчанию — профиль открывается всегда."""
    uid = _int(user_id, None) if user_id is not None else None
    if uid is None:
        return {}
    try:
        rows = (supabase_client.table(STATS_TABLE).select("*")
                .eq("user_id", uid).limit(1).execute().data) or []
    except Exception as exc:  # noqa: BLE001 — нет таблицы: профиль покажет «—»
        logger.debug("player_stats недоступны (user_id=%s): %s", uid, exc)
        return {}
    return rows[0] if rows else {}


def best_event(supabase_client, user_id, kind: str) -> dict | None:
    """Самый крупный «момент» нужного вида (плашка «Лучший апгрейд»)."""
    uid = _int(user_id, None) if user_id is not None else None
    if uid is None:
        return None
    try:
        rows = (supabase_client.table(EVENTS_TABLE)
                .select("title, amount, meta, created_at")
                .eq("user_id", uid).eq("kind", kind)
                .order("amount", desc=True).limit(1).execute().data) or []
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    row = rows[0]
    created = row.get("created_at")
    return {"title": row.get("title") or "", "amount": _int(row.get("amount"), 0),
            "meta": row.get("meta") or {},
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created}
