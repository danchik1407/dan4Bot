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
        "shop_image": "capsule_card.jpg",
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
        "shop_image": "uniq_capsule_card.jpg",
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
        "shop_image": "ruby_card.jpg",
        "inventory_image": None,
    },
    "diamond": {
        "name": "Алмазная капсула",
        "short_name": "Алмазная",
        "emoji": "💎",
        "price": 5000,
        "daily_limit": 5,
        "total_limit": 50,
        "admin_only": True,
        "shop_image": "diamond_card.jpg",
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
        "shop_image": "galactic_card.jpg",
        "inventory_image": None,
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
    """Снимок стока {item_id: остаток}. None — таблицы нет, лимиты не применяем."""
    try:
        response = supabase_client.table(STOCK_TABLE).select("item_id, stock").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Сток капсул недоступен: %s", exc)
        return None
    stock: dict[str, int] = {}
    for row in response.data or []:
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
    """JSON-ready catalog for the Mini App, already filtered for the viewer and the stock."""
    payload = []
    for item_id, item, left in available_shop_items(is_admin, stock):
        total_limit = item.get("total_limit")
        payload.append({
            "id": item_id,
            "name": item["name"],
            "short_name": item["short_name"],
            "emoji": item["emoji"],
            "price": int(item["price"]),
            "daily_limit": item["daily_limit"],
            "total_limit": int(total_limit) if total_limit is not None else None,
            "stock": left,
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


def _reserve_stock(supabase_client, item_id: str, current: int) -> bool:
    """Атомарно списывает 1 единицу. False — кто-то успел раньше (нужен повтор)."""
    try:
        updated = (
            supabase_client.table(STOCK_TABLE)
            .update({"stock": current - 1})
            .eq("item_id", item_id)
            .eq("stock", current)
            .select("item_id")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 — сток сломался, но покупку не отменяем
        logger.warning("Не удалось списать сток %s: %s", item_id, exc)
        return True
    return bool(updated.data)


def _release_stock(supabase_client, item_id: str, reserved_left: int) -> None:
    """Возвращает единицу стока, если покупку не удалось записать в профиль."""
    try:
        supabase_client.table(STOCK_TABLE).update({"stock": reserved_left + 1}).eq(
            "item_id", item_id).eq("stock", reserved_left).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось вернуть сток %s: %s", item_id, exc)


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

        # 1) Сначала резервируем единицу глобального тиража: если капсулу только что
        #    выкупили, повторяем попытку с новым остатком, а не продаём «минус одну».
        stock_available, stock_now = _read_stock_value(supabase_client, item_id)
        reserved_left = None
        if stock_available:
            if stock_now <= 0:
                raise ShopPurchaseError(SOLD_OUT_ERROR)
            if not _reserve_stock(supabase_client, item_id, stock_now):
                continue  # остаток изменился параллельной покупкой — читаем заново
            reserved_left = stock_now - 1

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
            if reserved_left is not None:
                _release_stock(supabase_client, item_id, reserved_left)
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
            }

        # 2) Списание с баланса не прошло (кнопку нажали дважды) — возвращаем тираж.
        if reserved_left is not None:
            _release_stock(supabase_client, item_id, reserved_left)

    raise ShopPurchaseError("Покупка обрабатывается. Обновите данные и попробуйте ещё раз.", 409)
