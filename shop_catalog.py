"""Shared shop catalog and purchase logic for the Mini App and Telegram bot."""

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Глобальный сток лимитированных капсул: одна строка на предмет, общая на всех игроков.
STOCK_TABLE = "capsule_stock"

# Каталог. total_limit — общий тираж на весь мир (как у NFT): когда сток кончился,
# карточка исчезает из магазина навсегда. daily_limit — старый дневной лимит.
SHOP_ITEMS = {
    "capsule": {
        "name": "Обычная капсула",
        "short_name": "Капсула",
        "emoji": "💊",
        "price": 100,
        "daily_limit": None,
        "total_limit": 1000,
        "admin_only": False,
        "shop_image": "cap_shop_normal.png",
        "inventory_image": "inv_capsule.png",
    },
    "uniq_capsule": {
        "name": "Уникальная капсула",
        "short_name": "Уник. капсула",
        "emoji": "⚱️",
        "price": 500,
        "daily_limit": 30,
        "total_limit": 500,
        "admin_only": False,
        "shop_image": "cap_shop_uniq.png",
        "inventory_image": "inv_uniq_capsule.png",
    },
    "ruby": {
        "name": "Рубиновая капсула",
        "short_name": "Рубиновая",
        "emoji": "♦️",
        "price": 1000,
        "daily_limit": None,
        "total_limit": 100,
        "admin_only": True,
        "shop_image": "cap_shop_ruby.png",
        "inventory_image": "inv_ruby.png",
    },
    "diamond": {
        "name": "Алмазная капсула",
        "short_name": "Алмазная",
        "emoji": "💎",
        "price": 5000,
        "daily_limit": 5,
        "total_limit": 50,
        "admin_only": True,
        "shop_image": "cap_shop_diamond.png",
        "inventory_image": "inv_diamond.png",
    },
    "galactic": {
        "name": "Галактическая капсула",
        "short_name": "Галактическая",
        "emoji": "🌌",
        "price": 10000,
        "daily_limit": None,
        "total_limit": 10,
        "admin_only": True,
        "shop_image": "cap_shop_galactic.png",
        "inventory_image": "inv_galactic.png",
    },
}

# Больше не продаются, но могли остаться в инвентаре у игроков — показываем в кармане.
LEGACY_ITEMS = {
    "malachite": {
        "name": "Малахитовая капсула",
        "short_name": "Малахитовая",
        "emoji": "🟢",
        "price": 3000,
        "daily_limit": 15,
        "total_limit": None,
        "admin_only": True,
        "shop_image": None,
        "inventory_image": "inv_malachite.png",
    },
    "emerald": {
        "name": "Изумрудная капсула",
        "short_name": "Изумрудная",
        "emoji": "💚",
        "price": 5000,
        "daily_limit": 10,
        "total_limit": None,
        "admin_only": True,
        "shop_image": None,
        "inventory_image": "inv_emerald.png",
    },
}

DAILY_LIMIT_ERROR = "Вы исчерпали дневной лимит на этот предмет"
SOLD_OUT_ERROR = "Товар распродан"
UNKNOWN_ITEM_ERROR = "Неизвестный предмет магазина"
SCHEMA_ERROR = (
    "Магазин временно недоступен: примените миграцию daily_buys и last_shop_reset в Supabase"
)


def admin_id_set(admin_ids: Any) -> set[int]:
    """Normalize an admin id list so string/int ids are compared reliably."""
    result: set[int] = set()
    for admin_id in admin_ids or []:
        try:
            result.add(int(admin_id))
        except (TypeError, ValueError):
            continue
    return result


def is_admin_id(user_id: Any, admin_ids: Any) -> bool:
    try:
        return int(user_id) in admin_id_set(admin_ids)
    except (TypeError, ValueError):
        return False


def visible_shop_items(is_admin: bool) -> dict[str, dict]:
    """Items a given viewer may buy at all (admin-only capsules stay hidden for everyone else)."""
    return {
        item_id: item
        for item_id, item in SHOP_ITEMS.items()
        if is_admin or not item.get("admin_only")
    }


def inventory_item(item_id: str) -> dict | None:
    """Данные предмета по id из инвентаря: в том числе снятые с продажи капсулы."""
    return SHOP_ITEMS.get(item_id) or LEGACY_ITEMS.get(item_id)


def visible_inventory_items(is_admin: bool) -> dict[str, dict]:
    """Что вообще можно показать в «кармане»: для неадминов новые капсулы скрыты."""
    items = dict(LEGACY_ITEMS)
    items.update(visible_shop_items(is_admin))
    return items


# ==================== ГЛОБАЛЬНЫЙ СТОК ====================
def _initial_stock_rows() -> list[dict]:
    return [
        {"item_id": item_id, "stock": int(item["total_limit"]), "total": int(item["total_limit"])}
        for item_id, item in SHOP_ITEMS.items()
        if item.get("total_limit") is not None
    ]


def ensure_stock_rows(supabase_client) -> bool:
    """Создаёт строки стока, которых не хватает. False — таблицы нет (миграция не применена)."""
    try:
        response = supabase_client.table(STOCK_TABLE).select("item_id").execute()
    except Exception as exc:  # noqa: BLE001 — таблицы может не быть до миграции
        logger.warning("Сток капсул недоступен: %s", exc)
        return False

    existing = {str(row.get("item_id")) for row in (response.data or [])}
    missing = [row for row in _initial_stock_rows() if row["item_id"] not in existing]
    if not missing:
        return True
    try:
        supabase_client.table(STOCK_TABLE).insert(missing).execute()
    except Exception as exc:  # noqa: BLE001 — параллельный воркер мог уже вставить строки
        logger.warning("Не удалось инициализировать сток капсул: %s", exc)
    return True


def load_stock(supabase_client) -> dict[str, int] | None:
    """Снимок стока {item_id: остаток}. None — таблицы нет, лимиты не применяем.

    Читаем через функцию shop_stock_list: она работает и при включённом RLS на
    таблице (иначе SELECT падал, магазин показывал полный тираж, а списание
    уходило «в никуда» — ровно тот баг, из-за которого счётчик не менялся).
    """
    rows = None
    try:
        rows = supabase_client.rpc("shop_stock_list", {}).execute().data
    except Exception as exc:  # noqa: BLE001
        logger.warning("RPC shop_stock_list недоступна (%s), читаем таблицу напрямую", exc)
    if rows is None:
        try:
            rows = supabase_client.table(STOCK_TABLE).select("item_id, stock").execute().data
        except Exception as exc:  # noqa: BLE001
            logger.warning("Сток капсул недоступен: %s", exc)
            return None
    stock: dict[str, int] = {}
    for row in rows or []:
        item_id = str(row.get("item_id") or "")
        if not item_id:
            continue
        try:
            stock[item_id] = max(0, int(row.get("stock") or 0))
        except (TypeError, ValueError):
            stock[item_id] = 0
    return stock


def shop_stock_snapshot(supabase_client) -> dict[str, int] | None:
    """Инициализация (если нужно) + чтение стока одним вызовом."""
    ensure_stock_rows(supabase_client)
    return load_stock(supabase_client)


def stock_left(item_id: str, item: dict, stock: dict[str, int] | None) -> int | None:
    """Сколько осталось. None — у предмета нет общего лимита или сток неизвестен."""
    total_limit = item.get("total_limit")
    if total_limit is None or stock is None:
        return None
    left = stock.get(item_id)
    if left is None:
        return int(total_limit)
    return max(0, int(left))


def available_shop_items(is_admin: bool, stock: dict[str, int] | None = None) -> list[tuple[str, dict, int | None]]:
    """(item_id, item, остаток) только для того, что реально можно купить сейчас."""
    result = []
    for item_id, item in visible_shop_items(is_admin).items():
        left = stock_left(item_id, item, stock)
        if left is not None and left <= 0:
            continue  # распродано — карточку не показываем вообще
        result.append((item_id, item, left))
    return result


def serialize_shop_items(is_admin: bool, stock: dict[str, int] | None = None) -> list[dict]:
    """JSON-ready catalog for the Mini App, already filtered for the viewer and the stock.

    stock/total всегда числа, когда у предмета есть тираж: без таблицы стока
    (миграция не применена) клиент показывал «Осталось» пустым, потому что
    приходило null. Теперь остаток падает обратно к тиражу, а stock_ready
    честно говорит, знаем ли мы реальный остаток.
    """
    payload = []
    for item_id, item, left in available_shop_items(is_admin, stock):
        total_limit = item.get("total_limit")
        total = int(total_limit) if total_limit is not None else None
        stock_left_value = left if left is not None else total
        payload.append({
            "id": item_id,
            "name": item["name"],
            "short_name": item["short_name"],
            "emoji": item["emoji"],
            "price": int(item["price"]),
            "daily_limit": item["daily_limit"],
            "total_limit": total,
            "stock": stock_left_value,
            "stock_total": total,
            "stock_ready": stock is not None,
            "shop_image": item["shop_image"],
            "inventory_image": item["inventory_image"],
        })
    return payload


class ShopPurchaseError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def parse_inventory(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _parse_daily_buys(raw: Any) -> dict[str, int]:
    if isinstance(raw, dict):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
    if not isinstance(parsed, dict):
        return {}

    counts: dict[str, int] = {}
    for item_id, count in parsed.items():
        try:
            counts[str(item_id)] = max(0, int(count))
        except (TypeError, ValueError):
            continue
    return counts


def _schema_error(exc: Exception) -> bool:
    message = str(exc)
    return "PGRST204" in message or "daily_buys" in message or "last_shop_reset" in message


def _read_stock_value(supabase_client, item_id: str) -> tuple[bool, int]:
    """(доступен ли сток, остаток). Сток недоступен — покупку не блокируем."""
    try:
        response = supabase_client.table(STOCK_TABLE).select("stock").eq("item_id", item_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Сток %s недоступен: %s", item_id, exc)
        return False, 0
    if not response.data:
        # Строки нет — создаём отсутствующие и перечитываем; гонка безопасна (insert if missing).
        ensure_stock_rows(supabase_client)
        try:
            response = supabase_client.table(STOCK_TABLE).select("stock").eq("item_id", item_id).execute()
        except Exception:  # noqa: BLE001
            return False, 0
        if not response.data:
            return False, 0
    try:
        return True, max(0, int(response.data[0].get("stock") or 0))
    except (TypeError, ValueError):
        return True, 0


# ==================== СПИСАНИЕ ТИРАЖА ====================
# Строгое списание — это SQL-функция shop_take_stock из миграции
# 20261102000000_stock_and_chest_rpc.sql:
#     UPDATE capsule_stock SET stock = stock - 1 WHERE item_id = ... AND stock > 0
# Один атомарный запрос вместо «прочитал 1000 → записал 999», поэтому две
# одновременные покупки не продают одну капсулу дважды и не затирают остаток.
STOCK_TAKE_RPC = "shop_take_stock"
STOCK_RETURN_RPC = "shop_return_stock"

# Признаки «функции/таблицы нет»: значит миграция не применена, списываем старым
# способом (условный UPDATE). Это НЕ повод отменять покупку.
_RPC_MISSING_MARKERS = (
    "PGRST202", "PGRST205", "404", "Could not find the function", "Could not find the table",
    "does not exist", "schema cache",
)
# Признаки «запись запрещена»: RLS, права, таймаут. Сток не обновится, и врать
# игроку «Осталось: 1000 из 1000» нельзя — сообщаем честно (stock_ready=false).
_STOCK_WRITE_BLOCKED = (
    "42501", "row-level security", "permission denied", "timeout", "timed out",
    "Connection", "JSON could not be generated", "503",
)


class StockSoldOut(Exception):
    """Тираж кончился: продавать нечего."""


class StockUnavailable(Exception):
    """Списать тираж нечем (нет таблицы/функции или запись запрещена).

    Покупку не отменяем — иначе магазин встанет целиком из-за одной таблицы,
    но и не делаем вид, что тираж уменьшился: вызывающий код вернёт
    stock_ready=false, и клиент не покажет фальшивое «Осталось: X из Y».
    """


def _stock_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _is_rpc_missing(exc: Exception) -> bool:
    message = _stock_error_text(exc)
    return any(marker in message for marker in _RPC_MISSING_MARKERS)


def _is_write_blocked(exc: Exception) -> bool:
    message = _stock_error_text(exc)
    return any(marker in message for marker in _STOCK_WRITE_BLOCKED)


def _rpc_call(supabase_client, name: str, params: dict):
    """Сырой вызов RPC. None — функции нет (нет миграции / другая сигнатура)."""
    try:
        return supabase_client.rpc(name, params).execute().data
    except Exception as exc:  # noqa: BLE001
        if _is_rpc_missing(exc):
            logger.warning("RPC %s(%s) недоступна: %s", name, list(params), exc)
            return _RPC_MISSING
        raise


class _RpcMissing:
    """Маркер «функции нет»: None — это валидный ответ RPC, путать нельзя."""


_RPC_MISSING = _RpcMissing()


def _call_take_stock(supabase_client, item_id: str):
    """Пробует все известные сигнатуры shop_take_stock.

    В разных SQL-миграциях функция объявлена по-разному:
      shop_take_stock(p_item_id text, p_amount integer)  — как просил игрок;
      shop_take_stock(p_item_id text)                    — вариант из 20261102.
    Поэтому перебираем параметры, а не падаем на первой же ошибке сигнатуры.
    Возвращает (нашли_ли_функцию, ответ, какие_параметры_сработали).
    """
    variants = (
        {"p_item_id": item_id, "p_amount": 1},
        {"p_item_id": item_id},
        {"item_id": item_id},
    )
    for params in variants:
        data = _rpc_call(supabase_client, STOCK_TAKE_RPC, params)
        if data is not _RPC_MISSING:
            return True, data, params
    return False, None, None


def _unwrap_rpc_value(data):
    """PostgREST отдаёт скаляр, [скаляр] или [{'имя_функции': скаляр}]."""
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        for key in ("remaining", "stock", "shop_take_stock", "result", "value"):
            if key in data:
                return data[key]
        data = next(iter(data.values()), None) if data else None
    return data


def _interpret_take_result(data) -> tuple[str, int | None]:
    """Приводим ответ функции к ('ok'|'sold_out'|'no_table'|'unknown', остаток).

    Поддержаны оба контракта:
      • integer — остаток после списания, -1 распродано, -2 нет таблицы;
      • boolean — True списано, False распродано (как просил игрок).
    """
    value = _unwrap_rpc_value(data)
    if isinstance(value, bool):
        return ("ok", None) if value else ("sold_out", 0)
    if isinstance(value, (int, float)):
        number = int(value)
        if number == -2:
            return "no_table", None
        if number == -1:
            return "sold_out", 0
        return "ok", max(0, number)
    # None / строка / что-то неожиданное: списание могло пройти — остаток перечитаем.
    return "unknown", None


def _read_stock_remaining(supabase_client, item_id: str) -> int | None:
    """Текущий остаток предмета: RPC → таблица. None — прочитать не удалось."""
    data = _rpc_call(supabase_client, "shop_stock_list", {})
    if data is not _RPC_MISSING:
        for row in data or []:
            if str(row.get("item_id")) == str(item_id):
                try:
                    return max(0, int(row.get("stock") or 0))
                except (TypeError, ValueError):
                    return 0
        return None
    available, current = _read_stock_value(supabase_client, item_id)
    return current if available else None


def reserve_stock(supabase_client, item_id: str) -> int | None:
    """Списывает одну единицу тиража. Возвращает остаток после списания.

    StockSoldOut — распродано (400 «Товар распродан»),
    StockUnavailable — списать не удалось (покупка идёт, но stock_ready=false).
    """
    try:
        found, data, params = _call_take_stock(supabase_client, item_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("ОШИБКА СТОКА: списание %s через RPC: %s", item_id, _stock_error_text(exc))
        raise StockUnavailable(_stock_error_text(exc)) from exc

    if found:
        verdict, left = _interpret_take_result(data)
        # Одна строка на покупку: видно и параметры вызова, и сырой ответ функции.
        print(f"СТОК {item_id}: rpc {STOCK_TAKE_RPC}{params} → {data!r} ({verdict})", flush=True)
        if verdict == "no_table":
            logger.error("ОШИБКА СТОКА: функция %s сообщила, что таблицы %s нет",
                         STOCK_TAKE_RPC, STOCK_TABLE)
            raise StockUnavailable("capsule_stock отсутствует")
        if verdict == "sold_out":
            print(f"СТОК {item_id}: распродан — отдаём «Товар распродан»", flush=True)
            raise StockSoldOut(item_id)
        if left is None:
            left = _read_stock_remaining(supabase_client, item_id)
        logger.info("Сток %s: -1 → %s", item_id, left)
        return left

    # Функции нет вообще (миграция не применена) — последний шанс: условный UPDATE.
    logger.warning("RPC %s не найдена — списываем условным UPDATE (старая схема)",
                   STOCK_TAKE_RPC)
    for _attempt in range(3):
        available, current = _read_stock_value(supabase_client, item_id)
        if not available:
            raise StockUnavailable("capsule_stock недоступна")
        if current <= 0:
            print(f"СТОК {item_id}: распродан (0)", flush=True)
            raise StockSoldOut(item_id)
        try:
            updated = (
                supabase_client.table(STOCK_TABLE)
                .update({"stock": current - 1})
                .eq("item_id", item_id)
                .eq("stock", current)
                .select("item_id")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("ОШИБКА СТОКА: списание %s: %s", item_id, _stock_error_text(exc))
            raise StockUnavailable(_stock_error_text(exc)) from exc
        if updated.data:
            print(f"СТОК {item_id}: списано 1, осталось {current - 1} (без RPC)", flush=True)
            return current - 1
    raise StockUnavailable("не удалось атомарно списать тираж")


def release_stock(supabase_client, item_id: str) -> None:
    """Возвращает единицу тиража, если покупку не удалось записать в профиль."""
    for params in ({"p_item_id": item_id, "p_amount": 1}, {"p_item_id": item_id}, {"item_id": item_id}):
        try:
            data = _rpc_call(supabase_client, STOCK_RETURN_RPC, params)
        except Exception as exc:  # noqa: BLE001
            logger.error("ОШИБКА СТОКА: возврат %s через RPC: %s", item_id, _stock_error_text(exc))
            data = _RPC_MISSING
        if data is _RPC_MISSING:
            continue
        verdict, left = _interpret_take_result(data)
        print(f"СТОК {item_id}: возврат 1{params} → {data!r} ({verdict}, осталось {left})", flush=True)
        return

    try:
        available, current = _read_stock_value(supabase_client, item_id)
        if not available:
            return
        updated = (
            supabase_client.table(STOCK_TABLE)
            .update({"stock": current + 1})
            .eq("item_id", item_id)
            .eq("stock", current)
            .execute()
        )
        if updated.data:
            print(f"СТОК {item_id}: возврат 1, осталось {current + 1} (без RPC)", flush=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("ОШИБКА СТОКА: возврат %s: %s", item_id, _stock_error_text(exc))


# ==================== ДИАГНОСТИКА ТИРАЖА ====================
def stock_diagnostics(supabase_client) -> dict:
    """Что реально происходит со стоком: есть ли RPC, что она возвращает, что в таблице.

    Вызов безопасен: функция зовётся с несуществующим item_id («__probe__») и
    ничего не списывает. Нужен, когда «миграцию применил, а счётчик не падает»:
    сразу видно, доходит ли дело до RPC и что база отвечает.
    """
    result = {
        "rpc": STOCK_TAKE_RPC,
        "return_rpc": STOCK_RETURN_RPC,
        "attempts": [],
        "table": {"readable": False, "error": None},
        "snapshot": None,
    }
    for params in ({"p_item_id": "__probe__", "p_amount": 1},
                   {"p_item_id": "__probe__"},
                   {"item_id": "__probe__"}):
        try:
            data = supabase_client.rpc(STOCK_TAKE_RPC, params).execute().data
        except Exception as exc:  # noqa: BLE001
            result["attempts"].append({
                "params": params,
                "ok": False,
                "error": _stock_error_text(exc)[:300],
                "function_missing": _is_rpc_missing(exc),
            })
            continue
        verdict, left = _interpret_take_result(data)
        result["attempts"].append({
            "params": params,
            "ok": True,
            "raw": repr(data)[:200],
            "verdict": verdict,        # sold_out — правильный ответ на пробный id
            "left": left,
        })
        break

    try:
        response = supabase_client.table(STOCK_TABLE).select("item_id, stock").execute()
        result["table"] = {"readable": True, "error": None, "rows": response.data or []}
    except Exception as exc:  # noqa: BLE001
        result["table"] = {"readable": False, "error": _stock_error_text(exc)[:300]}

    try:
        result["snapshot"] = load_stock(supabase_client)
    except Exception as exc:  # noqa: BLE001
        result["snapshot_error"] = _stock_error_text(exc)[:300]
    result["rpc_ready"] = any(item.get("ok") for item in result["attempts"])
    return result


def purchase_shop_item(supabase_client, user_id: int, item_id: str, admin_ids: list[int]) -> dict:
    """Validate and persist one purchase with optimistic protection against concurrent clicks."""
    numeric_user_id = int(user_id)
    is_admin = is_admin_id(numeric_user_id, admin_ids)

    item = SHOP_ITEMS.get(item_id)
    # Admin-only capsules must look non-existent to everyone else, so the store cannot be probed.
    if item is None or (item.get("admin_only") and not is_admin):
        raise ShopPurchaseError(UNKNOWN_ITEM_ERROR)

    price = int(item["price"])
    daily_limit = item["daily_limit"]

    # A conditional update on the previous balance/counters prevents simultaneous bot and
    # Mini App requests from overwriting one another or bypassing the daily cap.
    for _attempt in range(5):
        try:
            response = (
                supabase_client.table("users")
                .select("coins, inventory, daily_buys, last_shop_reset")
                .eq("user_id", numeric_user_id)
                .execute()
            )
        except Exception as exc:
            message = SCHEMA_ERROR if _schema_error(exc) else "Не удалось проверить данные магазина. Попробуйте ещё раз."
            raise ShopPurchaseError(message, 503) from exc

        if not response.data:
            raise ShopPurchaseError("Пользователь не найден", 404)

        user = response.data[0]
        raw_coins = user.get("coins", 0) or 0
        current_coins = int(raw_coins)
        raw_daily_buys = user.get("daily_buys") or "{}"
        raw_last_reset = user.get("last_shop_reset", 0) or 0
        today = int(time.time() // 86400)
        try:
            last_reset = int(raw_last_reset)
        except (TypeError, ValueError):
            last_reset = 0
        daily_buys = _parse_daily_buys(raw_daily_buys) if last_reset == today else {}

        current_count = daily_buys.get(item_id, 0)
        if not is_admin and daily_limit is not None and current_count >= daily_limit:
            raise ShopPurchaseError(DAILY_LIMIT_ERROR)
        if current_coins < price:
            raise ShopPurchaseError("Недостаточно коинов")

        # 1) Сначала списываем единицу глобального тиража — строго и до баланса.
        #    Распродано → 400 «Товар распродан»; списать не удалось → покупку не
        #    отменяем, но и остаток не выдумываем: stock_ready=false.
        stock_ready = True
        stock_taken = False          # списали единицу тиража → её надо вернуть при сбое
        reserved_left = None
        try:
            reserved_left = reserve_stock(supabase_client, item_id)
            stock_taken = True
        except StockSoldOut as exc:
            raise ShopPurchaseError(SOLD_OUT_ERROR) from exc
        except StockUnavailable as exc:
            logger.error("Покупка %s без списания тиража: %s", item_id, exc)
            stock_ready = False

        daily_buys[item_id] = current_count + 1
        inventory = parse_inventory(user.get("inventory"))
        inventory.append(item_id)
        new_coins = current_coins - price
        update_values = {
            "coins": new_coins,
            "inventory": json.dumps(inventory, ensure_ascii=False),
            "daily_buys": json.dumps(daily_buys, ensure_ascii=False),
            "last_shop_reset": today,
        }

        try:
            updated = (
                supabase_client.table("users")
                .update(update_values)
                .eq("user_id", numeric_user_id)
                .eq("coins", raw_coins)
                .eq("daily_buys", raw_daily_buys)
                .eq("last_shop_reset", raw_last_reset)
                .select("user_id")
                .execute()
            )
        except Exception as exc:
            if stock_taken:
                release_stock(supabase_client, item_id)
            message = SCHEMA_ERROR if _schema_error(exc) else "Не удалось завершить покупку. Попробуйте ещё раз."
            raise ShopPurchaseError(message, 503) from exc

        if updated.data:
            return {
                "success": True,
                "new_balance": new_coins,
                "inventory": inventory,
                "daily_buys": daily_buys,
                "last_shop_reset": today,
                "stock": reserved_left,
                "stock_ready": stock_ready,
            }

        # 2) Списание с баланса не прошло (кнопку нажали дважды) — возвращаем тираж.
        if stock_taken:
            release_stock(supabase_client, item_id)

    raise ShopPurchaseError("Покупка обрабатывается. Обновите данные и попробуйте ещё раз.", 409)
