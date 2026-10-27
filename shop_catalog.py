"""Shared shop catalog and purchase logic for the Mini App and Telegram bot."""

import json
import time
from typing import Any


SHOP_ITEMS = {
    "capsule": {
        "name": "Обычная капсула",
        "short_name": "Капсула",
        "emoji": "💊",
        "price": 500,
        "daily_limit": None,
        "admin_only": False,
        "shop_image": "capsule.png",
        "inventory_image": "inv_capsule.png",
    },
    "uniq_capsule": {
        "name": "Уникальная капсула",
        "short_name": "Уник. капсула",
        "emoji": "⚱️",
        "price": 1000,
        "daily_limit": 30,
        "admin_only": True,
        "shop_image": "uniq_capsule.png",
        "inventory_image": "inv_uniq_capsule.png",
    },
    "malachite": {
        "name": "Малахитовая капсула",
        "short_name": "Малахитовая",
        "emoji": "🟢",
        "price": 3000,
        "daily_limit": 15,
        "admin_only": True,
        "shop_image": "malachite.png",
        "inventory_image": "inv_malachite.png",
    },
    "emerald": {
        "name": "Изумрудная капсула",
        "short_name": "Изумрудная",
        "emoji": "💚",
        "price": 5000,
        "daily_limit": 10,
        "admin_only": True,
        "shop_image": "emerald.png",
        "inventory_image": "inv_emerald.png",
    },
    "diamond": {
        "name": "Алмазная капсула",
        "short_name": "Алмазная",
        "emoji": "💎",
        "price": 10000,
        "daily_limit": 5,
        "admin_only": True,
        "shop_image": "diamond.png",
        "inventory_image": "inv_diamond.png",
    },
}

DAILY_LIMIT_ERROR = "Вы исчерпали дневной лимит на этот предмет"
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
    """Items a given viewer is allowed to see. Admin-only capsules stay hidden for everyone else."""
    return {
        item_id: item
        for item_id, item in SHOP_ITEMS.items()
        if is_admin or not item.get("admin_only")
    }


def serialize_shop_items(is_admin: bool) -> list[dict]:
    """JSON-ready catalog for the Mini App, already filtered for the viewer."""
    payload = []
    for item_id, item in visible_shop_items(is_admin).items():
        payload.append({
            "id": item_id,
            "name": item["name"],
            "short_name": item["short_name"],
            "emoji": item["emoji"],
            "price": int(item["price"]),
            "daily_limit": item["daily_limit"],
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
            message = SCHEMA_ERROR if _schema_error(exc) else "Не удалось завершить покупку. Попробуйте ещё раз."
            raise ShopPurchaseError(message, 503) from exc

        if updated.data:
            return {
                "success": True,
                "new_balance": new_coins,
                "inventory": inventory,
                "daily_buys": daily_buys,
                "last_shop_reset": today,
            }

    raise ShopPurchaseError("Покупка обрабатывается. Обновите данные и попробуйте ещё раз.", 409)
