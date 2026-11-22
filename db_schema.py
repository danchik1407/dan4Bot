"""Аудит схемы Supabase: чего не хватает в базе и какой SQL выполнить.

Зачем отдельный модуль: одну и ту же проверку должны уметь и API (`/api/db_audit`,
Mini App) и бот (`!бд`, админ прямо в Telegram). Держать два списка колонок —
значит однажды их рассинхронизировать, поэтому список живёт здесь.

Ошибка «В базе не хватает колонки «created_at» в таблице users» раньше всегда
называла таблицу users, даже если колонки не хватало в subscriptions_history
или referrals. Теперь имя таблицы берётся из ответа PostgREST (см.
`api._sql_error_column_table`), а этот модуль показывает ВСЮ картину: какие
таблицы есть, каких колонок не хватает, что из этого ломает игру, а что просто
«украшение» (created_at), и готовый SQL.

Модуль ничего не импортирует из api.py/bot.py — его можно звать откуда угодно.
"""

from __future__ import annotations

# Какие колонки РЕАЛЬНО нужны коду. Проверяется запросом `select(col)`.
REQUIRED = {
    "users": ["user_id", "coins", "keys", "last_chest", "inventory", "created_at"],
    "subscriptions_history": ["user_id", "bonus_claimed", "reward", "created_at"],
    "passive_income": ["user_id", "last_at"],
    "referrals": ["referred_id", "referrer_id", "reward", "status", "created_at"],
    "stars_payments": ["charge_id", "user_id", "coins", "stars", "created_at"],
    "idempotency_keys": ["key", "user_id", "endpoint", "response", "created_at"],
    "game_settings": ["key", "value"],
    # bank нужен выплатам/RPC пары: без него бот писал «примените миграцию».
    "relationships": ["id", "user_a", "user_b", "love", "bank", "status", "created_at"],
    "love_stage_rewards": ["relationship_id", "stage"],
    "love_cooldowns": ["user_id", "action_at"],
    "solo_love": ["user_id", "love"],
    "solo_love_stage_rewards": ["user_id", "stage"],
    "crash_history": ["id", "round_id", "crash_point", "players", "created_at"],
    "upgrade_log": ["id", "user_id", "item_from", "item_to", "chance", "roll",
                    "won", "created_at"],
    "shop_purchases": ["id", "user_id", "item_id", "price", "created_at"],
    "chest_claims": ["id", "user_id", "claimed"],
    "capsule_stock": ["item_id", "stock", "total"],
    "bans": ["user_id"],
    "promocodes": ["code"],
    # Задача 57: предупреждения в чате. PGRST204 «Could not find the 'warns'
    # column of 'chat_warns'» ломал !аурапред — теперь аудит видит это сам.
    "chat_warns": ["chat_id", "user_id", "warns", "muted_until"],
    # «Мелочь» пассивного дохода (задача 56): без таблицы она теряется после
    # рестарта сервиса.
    "passive_carry": ["user_id", "carry"],
}

# Типы для SQL-подсказки: админ копирует ALTER TABLE целиком.
COLUMN_TYPES = {
    "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    "paid_at": "TIMESTAMPTZ",
    "claimed_at": "TIMESTAMPTZ",
    "last_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    "user_id": "BIGINT",
    "referred_id": "BIGINT",
    "referrer_id": "BIGINT",
    "coins": "INTEGER NOT NULL DEFAULT 0",
    "keys": "INTEGER NOT NULL DEFAULT 0",
    "last_chest": "INTEGER NOT NULL DEFAULT 0",
    "inventory": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "bonus_claimed": "BOOLEAN NOT NULL DEFAULT TRUE",
    "reward": "INTEGER NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT 'pending'",
    "bank": "INTEGER NOT NULL DEFAULT 0",
    "love": "INTEGER NOT NULL DEFAULT 0",
    "user_a": "BIGINT NOT NULL",
    "user_b": "BIGINT NOT NULL",
    "relationship_id": "BIGINT NOT NULL",
    "stage": "INTEGER NOT NULL DEFAULT 0",
    "chat_id": "BIGINT NOT NULL",
    "warns": "INTEGER NOT NULL DEFAULT 0",
    "muted_until": "TIMESTAMPTZ",
    "carry": "NUMERIC(10,6) NOT NULL DEFAULT 0",
    "action_at": "TIMESTAMPTZ NOT NULL DEFAULT now()",
    "stars": "INTEGER NOT NULL DEFAULT 0",
    "players": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "claimed": "BOOLEAN NOT NULL DEFAULT FALSE",
    "ice": "INTEGER NOT NULL DEFAULT 0",
    "premium": "BOOLEAN NOT NULL DEFAULT FALSE",
}

# Без этих колонок игра работает: теряется только «дней в игре»/метка времени.
OPTIONAL_COLUMNS = {"created_at", "updated_at", "paid_at", "claimed_at"}

# Какая миграция закрывает конкретную колонку (для подсказки в отчёте).
MIGRATION_BY_COLUMN = {
    "created_at": "20261028000000_created_at_and_crash_history.sql",
    "ice": "20261109000000_economy_social.sql",
    "premium": "20261109000000_economy_social.sql",
    "inventory": "20261027000000_shop_daily_limits.sql",
    # Задача 57: эти колонки (и solo_love) закрывает один файл — он же лечит
    # «Love не записался в базу» из скриншотов.
    "bank": "20261128000000_chat_warns_and_love_full.sql",
    "love": "20261128000000_chat_warns_and_love_full.sql",
    "status": "20261128000000_chat_warns_and_love_full.sql",
    "action_at": "20261128000000_chat_warns_and_love_full.sql",
    "relationship_id": "20261128000000_chat_warns_and_love_full.sql",
    "stage": "20261122000000_solo_love.sql",
    # Задача 57: один файл закрывает и предупреждения, и любовь.
    "chat_id": "20261128000000_chat_warns_and_love_full.sql",
    "warns": "20261128000000_chat_warns_and_love_full.sql",
    "muted_until": "20261128000000_chat_warns_and_love_full.sql",
    "carry": "20261128000000_chat_warns_and_love_full.sql",
}

RELOAD_HINT = ("Если миграция уже применена — обновите кэш схемы PostgREST: "
               "SQL Editor → NOTIFY pgrst, 'reload schema'; "
               "(Dashboard → Settings → API → Reload schema)")


def _table_missing(exc: Exception) -> bool:
    lower = f"{type(exc).__name__}: {exc}".lower()
    return ("pgrst205" in lower or "42p01" in lower
            or "could not find the table" in lower
            or ("does not exist" in lower and "table" in lower))


def _audit_table(client, table: str, columns: list) -> tuple:
    """(есть_таблица, список_недостающих_колонок) для одной таблицы."""
    try:
        client.table(table).select(",".join(columns)).limit(1).execute()
        return True, []
    except Exception as exc:  # noqa: BLE001
        if _table_missing(exc):
            return False, list(columns)
        missing = []
        for column in columns:
            try:
                client.table(table).select(column).limit(1).execute()
            except Exception as exc2:  # noqa: BLE001
                if _table_missing(exc2):
                    return False, list(columns)
                missing.append(column)
        return True, missing


def audit(client) -> dict:
    """Полный отчёт: таблицы, недостающие колонки, блокирующие ли они, SQL."""
    missing_tables, missing_columns = [], []
    errors = []
    for table, columns in REQUIRED.items():
        exists, missing = _audit_table(client, table, columns)
        if not exists:
            missing_tables.append(table)
            continue
        for column in missing:
            missing_columns.append({"table": table, "column": column,
                                    "optional": column in OPTIONAL_COLUMNS})
    blocking = [f"{item['table']}.{item['column']}" for item in missing_columns
                if not item["optional"]]
    ok = not missing_tables and not missing_columns
    return {
        "ok": ok,
        "checked_tables": len(REQUIRED),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "blocking": blocking,
        # Совместимость с жалобой «не хватает created_at в users».
        "users_created_at": not any(item["table"] == "users"
                                    and item["column"] == "created_at"
                                    for item in missing_columns),
        "sql": build_sql(missing_tables, missing_columns),
        "hint": ("Схема в порядке" if ok else
                 "Выполните sql в Supabase → SQL Editor и повторите"),
        "errors": errors,
    }


# Задача 57: в базе две известные «дыры» закрывает один готовый файл. Если
# аудит нашёл в них хоть что-то, подсказываем именно его, а не «примените
# миграции из папки» — админ должен получить копипастой.
FULL_MIGRATION_FILE = "supabase/migrations/20261128000000_chat_warns_and_love_full.sql"
FULL_MIGRATION_TABLES = {"chat_warns", "relationships", "solo_love",
                         "solo_love_stage_rewards", "love_stage_rewards",
                         "passive_carry"}


def build_sql(missing_tables, missing_columns) -> str:
    tables = list(missing_tables or [])
    columns = list(missing_columns or [])
    needs_full = bool(FULL_MIGRATION_TABLES & set(tables)) or any(
        item["table"] in FULL_MIGRATION_TABLES for item in columns)
    lines = []
    if needs_full:
        lines.append(f"-- Примените целиком: {FULL_MIGRATION_FILE}")
        lines.append("-- (там chat_warns.warns, relationships/solo_love, RLS и reload кэша)")
    for table in tables:
        if needs_full and table in FULL_MIGRATION_TABLES:
            continue
        lines.append(f"-- Таблицы {table} нет: примените миграции из supabase/migrations")
    for item in columns:
        table, column = item["table"], item["column"]
        if needs_full and table in FULL_MIGRATION_TABLES:
            continue
        col_type = COLUMN_TYPES.get(column, "TEXT")
        lines.append(f"ALTER TABLE public.{table} "
                     f"ADD COLUMN IF NOT EXISTS {column} {col_type};")
    if columns or needs_full:
        lines.append("")
        lines.append("NOTIFY pgrst, 'reload schema';")
    return "\n".join(lines)


def format_report(report: dict, limit: int = 24) -> str:
    """Короткий отчёт для Telegram (HTML) — плоский текст, без простыней."""
    if report.get("ok"):
        return (f"✅ <b>Схема базы в порядке</b>\n"
                f"Проверено таблиц: {report.get('checked_tables')}\n"
                f"users.created_at на месте, недостающих колонок нет.")
    parts = ["⚠️ <b>Схема базы неполная</b>",
             f"Проверено таблиц: {report.get('checked_tables')}"]
    if report.get("missing_tables"):
        parts.append("<b>Нет таблиц:</b> " + ", ".join(report["missing_tables"]))
    missing = report.get("missing_columns") or []
    if missing:
        shown = missing[:limit]
        lines = []
        for item in shown:
            mark = "•" if item.get("optional") else "❗"
            lines.append(f"{mark} {item['table']}.{item['column']}")
        parts.append("<b>Нет колонок:</b>\n" + "\n".join(lines))
        if len(missing) > limit:
            parts.append(f"…и ещё {len(missing) - limit}")
    if report.get("blocking"):
        parts.append("❗ — ломает игру, • — не критично (created_at и т.п.)")
    elif missing:
        parts.append("Все недостающие колонки не критичны для игры.")
    migration = MIGRATION_BY_COLUMN.get("created_at")
    parts.append(f"SQL: <code>{report.get('sql')[:900]}</code>")
    if migration:
        parts.append(f"created_at: supabase/migrations/{migration}")
    return "\n\n".join(parts)
