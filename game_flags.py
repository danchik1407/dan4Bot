"""Рубильники игр (задача 87).

Заказчик: «чтобы я мог отдельно временно закрывать какую-либо игру, может, одну,
может две сразу, может три, может все, и также их потом включать для всех
игроков» — то есть то же, что техработы, но по каждой игре отдельно.

Что здесь есть:

* список режимов (`MODES`) — один и тот же для админки в Mini App и для бота;
* чтение состояния из `game_settings` (ключ `game_disabled_<mode>`), потому что
  его читают ОБА сервиса Render — бот и api;
* короткий кэш, чтобы каждый запрос игрока не бил в базу;
* честные ответы: если режим закрыт, игрок видит «🛠️ <игра> временно
  выключена. Скоро включим — загляни позже», а админ играет как обычно.

Модуль без aiogram и без FastAPI: его зовут и api.py, и bot.py.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("aura-game-flags")

SETTINGS_TABLE = "game_settings"
KEY_PREFIX = "game_disabled_"
CACHE_TTL = 10.0

# Порядок = порядок кнопок в админке.
MODES = (
    ("mines", "💣 Мины"),
    ("crash", "🚀 Ракета"),
    ("arena_normal", "🏒 Арена (обычная)"),
    ("arena_team", "🔵 Арена (командная)"),
    ("upgrade", "🛠️ Апгрейд"),
    ("chests", "📦 Сундуки"),
    ("shop", "🛒 Магазин"),
    ("love", "💗 Love (РП)"),
)
MODE_IDS = tuple(mode for mode, _title in MODES)
TITLES = dict(MODES)

# «Арена» в аппке одна, но котлов два: игроку нужно закрыть оба сразу, когда
# сломан бросок. Поэтому у режима может быть несколько ключей.
ALIASES = {
    "arena": ("arena_normal", "arena_team"),
    "hockey": ("arena_normal", "arena_team"),
    "dice": ("dice",),
    "shop": ("shop",),
    "chests": ("chests",),
}

_cache = {"val": None, "at": 0.0}


def _as_bool(raw) -> bool:
    """Значение из базы: «1/вкл/true» = ВЫКЛЮЧЕНО, «0/выкл» = включено."""
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "вкл", "выкл", "выключено")


def read_disabled(client) -> dict:
    """{mode: True/False} — какие игры закрыты. Ошибка базы = всё открыто.

    Почему так: упавшая база не должна запирать игры всем. Если прочитать не
    удалось — отдаём последнее известное значение.
    """
    now = time.time()
    if _cache["val"] is not None and (now - float(_cache["at"] or 0)) < CACHE_TTL:
        return dict(_cache["val"])
    values = dict(_cache["val"] or {mode: False for mode in MODE_IDS})
    try:
        res = client.table(SETTINGS_TABLE).select("key, value").execute()
        found = {}
        for row in (res.data or []):
            key = str(row.get("key") or "")
            if key.startswith(KEY_PREFIX):
                found[key[len(KEY_PREFIX):]] = _as_bool(row.get("value"))
        for mode in MODE_IDS:
            values[mode] = bool(found.get(mode, False))
    except Exception as exc:  # noqa: BLE001 — без таблицы настроек игра открыта
        logger.debug("game_flags: настройки не прочитались (%s)", exc)
        return dict(values)
    _cache.update({"val": dict(values), "at": now})
    return dict(values)


def write_mode(client, mode: str, disabled: bool, cache_reset=None) -> bool:
    """Закрыть/открыть один режим. True — база приняла значение."""
    mode = str(mode or "").strip()
    if mode not in MODE_IDS:
        return False
    try:
        client.table(SETTINGS_TABLE).upsert(
            {"key": f"{KEY_PREFIX}{mode}", "value": "1" if disabled else "0"},
            on_conflict="key").execute()
    except Exception as exc:  # noqa: BLE001
        logger.error("game_flags: не удалось записать %s: %s", mode, exc)
        return False
    values = dict(_cache["val"] or {m: False for m in MODE_IDS})
    values[mode] = bool(disabled)
    _cache.update({"val": values, "at": time.time()})
    if cache_reset:
        try:
            cache_reset()
        except Exception:  # noqa: BLE001
            pass
    return True


def reset_cache() -> None:
    """Сбросить кэш (после записи настроек другим сервисом — не обязательно)."""
    _cache.update({"val": None, "at": 0.0})


def keys_for(mode: str) -> tuple:
    """Какие флаги проверить для запрошенного режима («arena» → оба котла)."""
    mode = str(mode or "").strip()
    if mode in ALIASES:
        return ALIASES[mode]
    return (mode,) if mode else ()


def closed(client, mode: str) -> bool:
    """Закрыт ли режим. «Арена» закрыта, если закрыт ЛЮБОЙ из котлов —
    иначе игрок ставил бы в тот, что ещё работает, и жалоба «арена не играется»
    осталась бы."""
    keys = keys_for(mode)
    if not keys:
        return False
    flags = read_disabled(client)
    return any(bool(flags.get(key)) for key in keys)


def message(mode: str) -> str:
    """Текст игроку. Короткий, человеческий, без канцелярита."""
    title = TITLES.get(str(mode or ""))
    if not title:
        for key in keys_for(mode):
            title = title or TITLES.get(key)
    name = (title or "Игра").split(" ", 1)[-1]
    # «Игра временно выключена», а «Арена / Мины / Ракета / Сундуки / Love /
    # Магазин» — множественного числа и женского рода: подставляем целую фразу,
    # чтобы текст читался по-русски, а не «Мины выключена».
    return f"🛠️ {name}: временно выключено. Скоро вернём — загляни позже."


def summary(client) -> dict:
    """Для админки и для команды бота: {mode: {'title': ..., 'disabled': ...}}."""
    flags = read_disabled(client)
    return {mode: {"title": TITLES[mode], "disabled": bool(flags.get(mode))}
            for mode in MODE_IDS}
