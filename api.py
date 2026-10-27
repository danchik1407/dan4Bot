from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import logging
import os, hashlib, hmac, json, time, random, uvicorn, datetime, math
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client
from typing import Optional
from shop_catalog import SHOP_ITEMS, ShopPurchaseError, is_admin_id, parse_inventory, purchase_shop_item, \
    serialize_shop_items

load_dotenv()

logger = logging.getLogger("aura-api")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])

if os.path.exists("miniapp"):
    app.mount("/miniapp", StaticFiles(directory="miniapp", html=True), name="miniapp")

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = [6338018270, 5184813347, 8890834384]
MAX_BET = 10000

# ==================== СУНДУКИ ====================
CHEST_COOLDOWN = 18000              # обычный сундук: 5 часов
CHEST_MIN_COINS, CHEST_MAX_COINS = 10, 30      # обычный сундук: 10-30 коинов
CHEST_KEY_CHANCE = 0.20             # 20% на золотой ключ вместо коинов
GOLD_MIN_COINS, GOLD_MAX_COINS = 60, 180       # золотой сундук (открывается ключом)
GOLD_KEY_BACK_CHANCE = 0.10         # 10% вернуть ключ

# ==================== МИНЫ ====================
MINES_CELLS = 16
MINES_MAX_COUNT = 5
MINES_MIN_BET = 10
# Шанс взрыва на первом клике = честный (мины/16) + надбавка за крупную ставку/5 мин.
MINES_RISKY_BET = 1000              # ставка выше 1000 коинов играет рискованнее
MINES_RISKY_BET_BONUS = 0.15
MINES_FIVE_MINES_BONUS = 0.10       # выбрано 5 мин
MINES_MAX_FIRST_CHANCE = 0.75
MINES_MULTIPLIERS = {
    1: [1.03, 1.10, 1.18, 1.28, 1.40, 1.55, 1.75, 2.00, 2.35, 2.80, 3.40, 4.20, 5.30, 7.00, 10.00],
    2: [1.06, 1.18, 1.35, 1.58, 1.88, 2.28, 2.82, 3.55, 4.60, 6.10, 8.30, 11.50, 16.50, 25.00, 40.00],
    3: [1.10, 1.30, 1.60, 2.00, 2.55, 3.35, 4.50, 6.20, 8.80, 13.00, 20.00, 32.00, 55.00, 100.00, 200.00],
    4: [1.15, 1.45, 1.90, 2.55, 3.50, 4.95, 7.20, 10.80, 17.00, 28.00, 48.00, 85.00, 160.00, 320.00, 700.00],
    5: [1.20, 1.60, 2.25, 3.20, 4.70, 7.20, 11.50, 19.00, 32.00, 58.00, 110.00, 220.00, 480.00, 1200.00, 3500.00],
}

# ==================== АПГРЕЙД (TONGRADER) ====================
# Шанс = (стоимость предмета слева / стоимость цели справа) * 100.
UPGRADE_MIN_BET = 50            # минимум коинов на левой панели
UPGRADE_MAX_BET = 5000          # максимум коинов на левой панели
UPGRADE_MAX_CAPSULES = 50       # максимум капсул за один апгрейд
UPGRADE_MIN_CHANCE = 0.5        # ниже 0.5% цель считается недостижимой
UPGRADE_MAX_CHANCE = 95.0       # 100% и выше не выдаём
UPGRADE_MAX_REWARD = 100000     # предохранитель по выплате
UPGRADE_COIN_MULTIPLIERS = [2, 4, 5, 8, 10]
UPGRADE_QUICK_CHANCES = [35, 55, 75]
UPGRADE_ITEM_PRICES = {"capsule": 500, "uniq_capsule": 1000}
UPGRADE_SOURCES = ("coin", "capsule", "uniq_capsule")
UPGRADE_TARGETS = ("coin", "capsule", "uniq_capsule")
# Пока играют только админы — шанс честный. Когда откроем для всех,
# реальный шанс = визуальный / этот делитель (то есть x2 = 25%).
UPGRADE_NON_ADMIN_DIVIDER = 2

BOT_NAMES = ["Shark_V", "LuckyFox", "NeonWolf", "GhostByte", "PixelKing", "CyberCat", "IronFist", "VoidWalker",
             "ShadowAce", "RogueOne", "Toxic", "Viper", "Sniper", "ProGamer", "NoobMaster", "Killer", "Dragon"]
BOT_COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#5856d6", "#af52de", "#ff2d55"]
WHALE_NAMES = ["KingPin", "MrMoney", "WhaleGod", "BigBoss", "RichMan", "GoldFinger"]
WHALE_COLORS = ["#ffd700", "#c0c0c0", "#b8860b", "#ff8c00", "#8b0000", "#daa520"]


def _random_arena_bot_count():
    # Keep most rounds light; large bot rooms are occasional.
    return random.randint(1, 3) if random.random() < 0.85 else random.randint(4, 10)


# ==================== ОБЩИЕ ХЕЛПЕРЫ ЭКОНОМИКИ ====================
def _safe_int(value, default=0):
    """NULL / строка / float из базы не должны ронять эндпоинт в 500."""
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _row_matches_id(row, user_id):
    raw = row.get("user_id")
    if raw is None:
        return False
    return str(raw).strip() == str(user_id).strip()


def _find_user_rows(user_id):
    """Ищем строку и по int, и по строке: в старой базе user_id мог сохраниться текстом."""
    uid = _safe_int(user_id, None)
    if uid is None:
        return []
    rows = []
    for key in (uid, str(uid)):
        try:
            res = supabase.table("users").select("*").eq("user_id", key).execute()
        except Exception:
            logger.exception("Не удалось прочитать users по user_id=%s", key)
            continue
        for row in res.data or []:
            if _row_matches_id(row, uid):
                rows.append(row)
        # Второй, «строковый» запрос нужен только если обычный ничего не нашёл.
        if rows:
            break
    unique = {}
    for row in rows:
        unique[str(row.get("id", "uid:%s" % row.get("user_id")))] = row
    return list(unique.values())


def _db_user(user_id):
    """Одна запись users или None. user_id приводим к int — в базе это bigint."""
    rows = _find_user_rows(user_id)
    return rows[0] if rows else None


def _db_update_user(user_id, **fields):
    """Обновляем только переданные колонки (никаких PGRST204 из-за лишних полей)."""
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        return False
    uid = _safe_int(user_id, None)
    if uid is None:
        return False
    # Строка могла быть сохранена как int, а могла — как текст (старые записи).
    for key in (uid, str(uid)):
        try:
            updated = supabase.table("users").update(payload).eq("user_id", key).select("user_id").execute()
        except Exception:
            logger.exception("Не удалось обновить users по user_id=%s", key)
            continue
        if updated.data:
            return True
    return False


def _user_payload(row, is_admin):
    """Единый ответ профиля: любые NULL/строки из базы приводим к нормальным типам."""
    created_at = row.get("created_at")
    if isinstance(created_at, str):
        created_text = created_at
    elif created_at is not None:
        try:
            created_text = created_at.isoformat()
        except AttributeError:
            created_text = str(created_at)
    else:
        created_text = None
    return {
        "ok": True,
        "coins": _safe_int(row.get("coins"), 0),
        "keys": _safe_int(row.get("keys"), 0),
        "inventory": parse_inventory(row.get("inventory")),
        "last_chest": _safe_int(row.get("last_chest"), 0),
        "is_admin": is_admin,
        "created_at": created_text,
        "days_in_game": _days_in_game(created_at),
    }


def _days_in_game(created_at):
    """Сколько полных дней прошло с регистрации. Нет даты — 0 дней."""
    if not created_at:
        return 0
    if isinstance(created_at, str):
        try:
            created = datetime.datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0
    elif isinstance(created_at, datetime.datetime):
        created = created_at
    else:
        return 0
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    delta = datetime.datetime.now(datetime.timezone.utc) - created
    return max(0, int(delta.total_seconds() // 86400))


def _is_admin_user(user_id):
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


def _enforce_bet_limit(user_id, total_bet):
    """Строгая серверная проверка общей ставки: > MAX_BET запрещено всем, кроме админов."""
    if _is_admin_user(user_id):
        return
    if int(total_bet) > MAX_BET:
        raise HTTPException(status_code=400, detail=f"Максимальная ставка {MAX_BET} 🪙")


# === АРЕНА ===
game_state = {
    "status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
    "last_update": time.time(), "last_bot_added": time.time(), "target_bots": _random_arena_bot_count(), "is_paid": False,
    "payout_attempt_at": 0, "round_id": str(int(time.time())), "whales_added": 0,
    "roll_data": None, "rolling_started_at": None, "whale_decided": False,
    "whale_join": False, "whale_trigger_at": None
}
history = []


HOCKEY_ROLL_DURATION = 9.0
HOCKEY_WAIT_DURATION = 15.0
HOCKEY_ROUND_PERIOD = HOCKEY_WAIT_DURATION + HOCKEY_ROLL_DURATION + 5.0
HOCKEY_SIMULATION_STEP = 0.05
HOCKEY_SPEED_EASE_POWER = 1.7   # чем больше — тем резвее старт и мягче финиш


def _hockey_speed_factor(elapsed_seconds):
    """Мяч стартует на полной скорости и плавно гаснет до нуля к концу раунда."""
    progress = min(1.0, max(0.0, elapsed_seconds / HOCKEY_ROLL_DURATION))
    return max(0.0, (1.0 - progress) ** HOCKEY_SPEED_EASE_POWER)


def _simulate_hockey_roll(start_angle, speed, start_x=50.0, start_y=50.0):
    """Precompute the authoritative path once; clients only render these server points."""
    x, y = float(start_x), float(start_y)
    direction_x = math.cos(start_angle)
    direction_y = math.sin(start_angle)
    base_vx = direction_x * speed
    base_vy = direction_y * speed
    steps = round(HOCKEY_ROLL_DURATION / HOCKEY_SIMULATION_STEP)
    path = [[round(x, 6), round(y, 6)]]

    for step in range(steps):
        elapsed = (step + 1) * HOCKEY_SIMULATION_STEP
        speed_factor = _hockey_speed_factor(elapsed)
        vx = math.copysign(abs(base_vx) * speed_factor, direction_x)
        vy = math.copysign(abs(base_vy) * speed_factor, direction_y)
        x += vx
        y += vy
        if vx and (x <= 0 or x >= 100):
            direction_x *= -1
            x = max(0.0, min(100.0, x))
        if vy and (y <= 0 or y >= 100):
            direction_y *= -1
            y = max(0.0, min(100.0, y))
        path.append([round(x, 6), round(y, 6)])

    return path


def _prepare_hockey_roll(now):
    """Create a deterministic-per-round path shared even across API workers."""
    # All workers use the same time-slot round id and RNG seed, rather than process-local randomness.
    round_slot = int(now // HOCKEY_ROUND_PERIOD)
    game_state["round_id"] = str(round_slot)
    round_rng = random.Random(round_slot)
    start_angle = round_rng.uniform(0.0, 2 * math.pi)
    # Скорость подобрана под ease-out: суммарный путь примерно как у прежней модели.
    speed = 18.0
    start_x, start_y = 50.0, 50.0
    path = _simulate_hockey_roll(start_angle, speed, start_x, start_y)
    game_state["winner"] = None
    game_state["rolling_started_at"] = now
    game_state["roll_data"] = {
        "start_angle": start_angle,
        "speed": speed,
        "start_x": start_x,
        "start_y": start_y,
        "final_x": path[-1][0],
        "final_y": path[-1][1],
        "path": path,
    }


def _update_hockey_percentages():
    total = sum(max(0, int(player.get("bet", 0))) for player in game_state["players"])
    game_state["bank"] = total
    for player in game_state["players"]:
        player["percent"] = (max(0, int(player.get("bet", 0))) / total * 100) if total else 0


def _unique_hockey_color(palette):
    used = {player.get("color") for player in game_state["players"]}
    available = [color for color in palette if color not in used]
    if available:
        return random.choice(available)
    return f"hsl({random.randint(0, 360)}, 80%, 50%)"


def _add_hockey_bot(is_whale=False):
    if len(game_state["players"]) >= 20:
        return False

    if is_whale:
        name = "Лудик" + str(random.randint(1, 99))
        bet = random.randint(500, 2000)
        color = _unique_hockey_color(WHALE_COLORS)
    else:
        name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
        bet = random.randint(10, 100)
        color = _unique_hockey_color(BOT_COLORS)

    game_state["players"].append({
        "id": f"bot_{random.randint(10000, 99999)}", "name": name, "avatar": "",
        "bet": bet, "color": color, "is_bot": True,
    })
    _update_hockey_percentages()
    return True


def _maybe_add_hockey_bots(now, time_left):
    if time_left <= 0:
        return

    current_bots = sum(1 for player in game_state["players"] if player.get("is_bot") and player.get("bet", 0) < 500)
    if (current_bots < game_state["target_bots"] and len(game_state["players"]) < 20
            and now - game_state["last_bot_added"] >= random.uniform(0.6, 1.5)):
        if _add_hockey_bot():
            game_state["last_bot_added"] = now

    if (game_state["whale_join"] and not game_state["whales_added"]
            and time_left <= game_state["whale_trigger_at"] and len(game_state["players"]) < 20):
        if _add_hockey_bot(is_whale=True):
            game_state["whales_added"] = 1


def _hockey_winner_at_x(x):
    """Resolve a final ball x-coordinate to the bet-weighted arena sector."""
    players = game_state["players"]
    total_bets = sum(max(0, int(player.get("bet", 0))) for player in players)
    if total_bets <= 0:
        return None

    current_x = 0.0
    for player in players:
        sector_width = max(0, int(player.get("bet", 0))) / total_bets * 100.0
        if current_x <= x <= current_x + sector_width + 0.0001:
            return player
        current_x += sector_width
    return players[-1] if players else None


def _payout_hockey_winner():
    """Credit the full displayed pot to a real winner, retrying failed DB writes."""
    winner = game_state["winner"]
    if game_state["is_paid"] or not winner:
        return
    if winner.get("is_bot"):
        game_state["is_paid"] = True
        return

    now = time.time()
    if now - game_state.get("payout_attempt_at", 0) < 2:
        return
    game_state["payout_attempt_at"] = now
    try:
        numeric_winner_id = int(winner["id"])
    except (TypeError, ValueError):
        # Non-numeric IDs belong to synthetic participants, never to user accounts.
        game_state["is_paid"] = True
        return

    try:
        res = supabase.table("users").select("coins").eq("user_id", numeric_winner_id).execute()
        if not res.data:
            raise RuntimeError("Победитель Арены не найден в таблице users")
        new_coins = int(res.data[0].get("coins", 0)) + int(game_state["bank"])
        updated = supabase.table("users").update({"coins": new_coins}).eq(
            "user_id", numeric_winner_id).select("user_id").execute()
        if not updated.data:
            raise RuntimeError("Supabase не подтвердил выплату победителю Арены")
        game_state["is_paid"] = True
    except Exception as e:
        # Keep the round unsettled so the background worker can retry instead of losing the payout.
        print(f"❌ Ошибка выплаты Арены: {e}")


def _finish_hockey_round():
    """Resolve from the server-simulated final x and pay exactly once."""
    if game_state["status"] != "rolling":
        return False
    # The winner receives the entire displayed pot, including bot wagers.
    game_state["bank"] = sum(int(player["bet"]) for player in game_state["players"])
    roll_data = game_state.get("roll_data") or {}
    if not game_state["players"] or game_state["bank"] <= 0 or "final_x" not in roll_data:
        game_state["winner"] = None
    else:
        game_state["winner"] = _hockey_winner_at_x(float(roll_data["final_x"]))

    game_state["status"] = "finished"
    game_state["last_update"] = time.time()
    if game_state["winner"]:
        history.insert(0, {
            "round_id": game_state["round_id"],
            "winner_name": game_state["winner"]["name"],
            "amount": game_state["bank"],
            "players": [
                {"name": p["name"], "bet": p["bet"], "is_winner": p["id"] == game_state["winner"]["id"]}
                for p in game_state["players"]
            ],
        })
        del history[10:]
        _payout_hockey_winner()
    return True


@app.on_event("startup")
async def start_hockey_settlement_worker():
    async def settle_expired_rounds():
        while True:
            await asyncio.sleep(0.2)
            if game_state["status"] == "rolling" and time.time() - game_state["last_update"] >= HOCKEY_ROLL_DURATION:
                _finish_hockey_round()
            elif game_state["status"] == "finished" and game_state.get("winner") and not game_state["is_paid"]:
                _payout_hockey_winner()

    asyncio.create_task(settle_expired_rounds())


# === РАКЕТА (CRASH) ===
CRASH_WAIT = 10
CRASH_READY = 3
CRASH_CRASHED = 5
crash_state = {
    "status": "waiting", "timer": CRASH_WAIT, "multiplier": 1.0, "last_update": time.time(),
    "crash_point": 0.0, "players": [], "bank": 0, "is_paid": False, "round_id": str(int(time.time())),
    "bots_added": 0, "target_bots": 0, "last_bot_added": 0, "status_start": time.time(),
}
# Глобальная история ракет: живёт в таблице crash_history, поэтому не сбрасывается,
# когда из игры выходят игроки или перезапускается воркер. Локальный список — кэш.
CRASH_HISTORY_LIMIT = 20
CRASH_HISTORY_CACHE_TTL = 5.0
crash_history = []
crash_history_cache_at = 0.0


def _crash_history_to_payload(row):
    try:
        crash_point = round(float(row.get("crash_point") or 0), 2)
    except (TypeError, ValueError):
        crash_point = 0.0
    return {"round_id": row.get("round_id"), "crash_point": crash_point,
            "created_at": row.get("created_at")}


def _load_crash_history_from_db(limit=CRASH_HISTORY_LIMIT):
    """None — база недоступна (миграция не применена), иначе список последних игр."""
    try:
        res = (supabase.table("crash_history")
               .select("round_id, crash_point, created_at")
               .order("id", desc=True)
               .limit(limit)
               .execute())
    except Exception:
        return None
    return [_crash_history_to_payload(row) for row in (res.data or [])]


def _refresh_crash_history(force=False):
    global crash_history, crash_history_cache_at
    now = time.time()
    if not force and (now - crash_history_cache_at) < CRASH_HISTORY_CACHE_TTL:
        return crash_history
    rows = _load_crash_history_from_db()
    if rows is not None:
        crash_history = rows
    crash_history_cache_at = now
    return crash_history


def _save_crash_history(entry):
    """Пишем раунд в базу и сразу добавляем в кэш, чтобы история была видна всем."""
    global crash_history, crash_history_cache_at
    record = {"round_id": entry.get("round_id"),
              "crash_point": round(float(entry.get("crash_point") or 0), 2),
              "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    crash_history.insert(0, record)
    del crash_history[CRASH_HISTORY_LIMIT:]
    crash_history_cache_at = time.time()
    try:
        supabase.table("crash_history").insert({
            "round_id": record["round_id"], "crash_point": record["crash_point"],
            "players": entry.get("players") or [],
        }).execute()
    except Exception:
        # База недоступна — играть не мешаем, история останется в памяти процесса.
        pass


def verify_telegram_data(init_data: str):
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data: return None
        hash_ = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == hash_: return json.loads(parsed_data.get("user", "{}"))
        return None
    except:
        return None


def check_ban(user_id: str):
    res = supabase.table("bans").select("*").eq("user_id", user_id).execute()
    if res.data: raise HTTPException(status_code=403, detail="Вы забанены за нарушение правил.")


def _crash_reset_waiting(now):
    global crash_state
    crash_state = {
        "status": "waiting", "timer": CRASH_WAIT, "multiplier": 1.0, "last_update": now,
        "crash_point": 0.0, "players": [], "bank": 0, "is_paid": False, "round_id": str(int(time.time())),
        "bots_added": 0, "target_bots": 0, "last_bot_added": 0, "status_start": now,
    }


class UserAuth(BaseModel): initData: str


class UpdateBalance(BaseModel): initData: str; amount: int


class UpdateKeys(BaseModel): initData: str; amount: int


class BuyItem(BaseModel): initData: str; item_id: str; price: Optional[int] = None


class ShopCatalogRequest(BaseModel): initData: str


class PlaceBet(BaseModel): initData: str; amount: int


class FinishHockey(BaseModel): initData: str; x: float; y: float


class CrashBet(BaseModel): initData: str; amount: int


class CrashCashout(BaseModel): initData: str


class MinesStart(BaseModel): initData: str; amount: int; mines: int = 1


class MinesOpen(BaseModel): initData: str; cell: int


class MinesCashout(BaseModel): initData: str


class OpenChestRequest(BaseModel):
    initData: str
    chest_type: str = "normal"


class UpgradeLogRequest(BaseModel): initData: str


class UpgradeAck(BaseModel):
    initData: str
    log_id: Optional[int] = None


class UpgradeAction(BaseModel):
    initData: str
    # Новый контракт: что ставим (item_from) и что хотим получить (item_to).
    item_from: Optional[str] = None
    amount_from: Optional[int] = None
    item_to: Optional[str] = None
    target_multiplier: Optional[float] = None
    target_chance: Optional[float] = None
    # Запасной вариант для старого закешированного клиента (bet_type/amount/quantity).
    bet_type: Optional[str] = None
    amount: Optional[int] = None
    quantity: Optional[int] = None


# ==================== АРЕНА (ХОККЕЙ) ====================
def _hockey_state_response():
    response = game_state.copy()
    # Derive the public pot from the same participants shown in the arena, including bots.
    response["bank"] = sum(max(0, int(player.get("bet", 0))) for player in response["players"])
    if response.get("roll_data"):
        response["roll_data"] = {key: value for key, value in response["roll_data"].items() if key != "path"}
    response["server_time"] = time.time()
    return response


@app.get("/api/hockey_state")
async def get_hockey_state():
    global game_state, history
    now = time.time()

    if game_state["status"] == "waiting":
        time_left = 15 - (now - game_state["last_update"])
        _maybe_add_hockey_bots(now, time_left)
        if time_left <= 0:
            # Freeze the full pot, including bot wagers.
            game_state["bank"] = sum(int(player["bet"]) for player in game_state["players"])
            game_state["status"] = "rolling"
            game_state["last_update"] = now
            _update_hockey_percentages()
            _prepare_hockey_roll(now)

    elif game_state["status"] == "rolling":
        if now - game_state["last_update"] >= HOCKEY_ROLL_DURATION:
            # Fallback is server-authoritative: leaving the app cannot cancel the draw or payout.
            _finish_hockey_round()

    elif game_state["status"] == "finished":
        if game_state.get("winner") and not game_state["is_paid"]:
            _payout_hockey_winner()
        if now - game_state["last_update"] > 5 and (not game_state.get("winner") or game_state["is_paid"]):
            game_state = {"status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
                          "last_update": now, "last_bot_added": now, "target_bots": _random_arena_bot_count(),
                          "is_paid": False, "payout_attempt_at": 0, "round_id": str(int(time.time())), "whales_added": 0,
                          "roll_data": None, "rolling_started_at": None, "whale_decided": False,
                          "whale_join": False, "whale_trigger_at": None}
            return _hockey_state_response()

    time_left = 15 - (now - game_state["last_update"]) if game_state["status"] == "waiting" else 0
    game_state["timer"] = max(0, int(time_left))
    return _hockey_state_response()


@app.get("/api/hockey_roll/{round_id}")
async def get_hockey_roll_path(round_id: str):
    roll_data = game_state.get("roll_data")
    if (game_state["round_id"] != round_id or not roll_data
            or game_state["status"] not in {"rolling", "finished"}):
        raise HTTPException(status_code=404, detail="Траектория раунда не найдена")
    return {"round_id": round_id, "path": roll_data["path"]}


@app.post("/api/place_bet")
async def place_bet(data: PlaceBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])

    check_ban(user_id)

    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше нуля")
    if (game_state["status"] != "waiting"
            or time.time() - game_state["last_update"] >= 15):
        raise HTTPException(status_code=400, detail="Ставки закрыты")
    existing_player = next(
        (p for p in game_state["players"] if p["id"] == user_id and not p.get("is_bot")),
        None,
    )
    # Лимит считается по ОБЩЕЙ ставке игрока в раунде, а не по одному запросу —
    # иначе он обходится десятью ставками по 1 000.
    total_after_bet = int(existing_player["bet"] if existing_player else 0) + int(data.amount)
    _enforce_bet_limit(user_id, total_after_bet)
    if existing_player is None and len(game_state["players"]) >= 20:
        raise HTTPException(status_code=400, detail="Комната заполнена")

    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    new_coins = current_coins - data.amount
    updated = supabase.table("users").update({"coins": new_coins}).eq("user_id", int(user_id)).select("user_id").execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Не удалось списать ставку")

    if existing_player is not None:
        existing_player["bet"] += data.amount
    else:
        name = user.get("username", user.get("first_name", f"id{user_id}"))
        avatar = user.get("photo_url", "")
        player_color = "#007aff"
        while any(p["color"] == player_color for p in game_state["players"]):
            player_color = f"hsl({random.randint(0, 360)}, 80%, 50%)"
        existing_player = {
            "id": user_id, "name": name, "avatar": avatar, "bet": data.amount,
            "color": player_color, "is_bot": False,
        }
        game_state["players"].append(existing_player)

    # One 10% chance per round when a human's accumulated bet reaches 1,000.
    if existing_player["bet"] >= 1000 and not game_state["whale_decided"]:
        game_state["whale_decided"] = True
        game_state["whale_join"] = random.random() < 0.10
        if game_state["whale_join"]:
            game_state["whale_trigger_at"] = random.uniform(1.0, 2.0)

    # The displayed bank and payout use the same full pot.
    game_state["bank"] = sum(int(player["bet"]) for player in game_state["players"])
    _update_hockey_percentages()
    return {"success": True, "new_balance": new_coins}


@app.post("/api/finish_hockey")
async def finish_hockey(data: FinishHockey):
    # Do not let a client decide or prematurely finish the round using its local animation.
    if game_state["status"] != "rolling":
        return {"success": False}
    if time.time() - game_state["last_update"] < HOCKEY_ROLL_DURATION:
        return {"success": False, "detail": "Раунд ещё идёт"}

    _finish_hockey_round()
    return {"success": True, "winner": game_state["winner"], "bank": game_state["bank"]}


@app.get("/api/hockey_history")
async def get_hockey_history(): return history


# ==================== РАКЕТА (CRASH) ====================
def get_crash_point_by_chance():
    """Мгновенные краши x1.00-x1.06 выпадают редко (12%), а не в трети раундов."""
    rv = random.random()
    if rv < 0.12:
        return random.uniform(1.00, 1.06)
    elif rv < 0.46:
        return random.uniform(1.06, 2.0)
    elif rv < 0.85:
        return random.uniform(2.0, 4.0)
    elif rv < 0.99:
        return random.uniform(4.0, 50.0)
    else:
        return 100.0


@app.get("/api/crash_state")
async def get_crash_state(initData: str = ""):
    global crash_state
    now = time.time()

    if crash_state["status"] == "waiting":
        elapsed = now - crash_state["status_start"]
        left = CRASH_WAIT - elapsed
        crash_state["timer"] = max(0, int(left))
        if left <= 8 and crash_state["target_bots"] == 0: crash_state["target_bots"] = random.randint(1, 3)
        if left <= 8 and crash_state["target_bots"] > 0:
            current_bots = sum(1 for p in crash_state["players"] if p["is_bot"])
            if current_bots < crash_state["target_bots"] and len(crash_state["players"]) < 20 and (
                    now - crash_state["last_bot_added"]) > 1.0:
                bot_bet = random.randint(10, 300)
                if random.random() < 0.8: bot_bet = random.randint(10, 100)
                bot_name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
                crash_state["players"].append(
                    {"id": f"bot_{random.randint(1000, 9999)}", "name": bot_name, "bet": bot_bet, "cashout_mult": 0,
                     "is_bot": True, "avatar": "", "auto_mult": 0})
                crash_state["bank"] += bot_bet
                crash_state["bots_added"] += 1
                crash_state["last_bot_added"] = now
        if left <= 0:
            crash_state["status"] = "ready"
            crash_state["status_start"] = now
            crash_state["last_update"] = now

    elif crash_state["status"] == "ready":
        elapsed = now - crash_state["status_start"]
        left = CRASH_READY - elapsed
        crash_state["timer"] = max(0, int(left))
        if left <= 0:
            crash_state["status"] = "flying"
            crash_state["status_start"] = now
            crash_state["last_update"] = now
            crash_state["multiplier"] = 1.0
            crash_state["crash_point"] = get_crash_point_by_chance()

    elif crash_state["status"] == "flying":
        elapsed = now - crash_state["status_start"]

        if elapsed <= 2.0:
            t = elapsed / 2.0
            current_mult = 1.0 + (t ** 2) * 0.2
        else:
            current_mult = 1.2 + (elapsed - 2.0) * 0.15 + math.pow(elapsed - 2.0, 1.2) * 0.05

        crash_state["multiplier"] = current_mult

        if current_mult >= crash_state["crash_point"]:
            crash_state["multiplier"] = crash_state["crash_point"]
            crash_state["status"] = "crashed"
            crash_state["status_start"] = now
            crash_state["last_update"] = now

            if not crash_state["is_paid"]:
                crash_state["is_paid"] = True
                for p in crash_state["players"]:
                    if p["cashout_mult"] > 0 and not p["is_bot"]:
                        pass  # УЖЕ ЗАБРАЛ - НЕ ПЛАТИМ ПОВТОРНО
                    elif p["cashout_mult"] == 0 and not p["is_bot"]:
                        pass  # ПРОИГРАЛ - НЕ ПЛАТИМ

            _save_crash_history({
                "round_id": crash_state["round_id"],
                "crash_point": round(crash_state["crash_point"], 2),
                "players": [{"name": p["name"], "bet": p["bet"], "cashout": p["cashout_mult"],
                             "is_bot": p["is_bot"]} for p in crash_state["players"]],
            })

        for p in crash_state["players"]:
            if p["cashout_mult"] == 0 and p["is_bot"] and random.random() < 0.05:
                p["cashout_mult"] = crash_state["multiplier"]

    elif crash_state["status"] == "crashed":
        elapsed = now - crash_state["status_start"]
        left = CRASH_CRASHED - elapsed
        crash_state["timer"] = max(0, int(left))
        if left <= 0:
            _crash_reset_waiting(now)
            return _crash_response(initData)

    return _crash_response(initData)


def _crash_response(initData: str):
    # Значения по умолчанию выставляем сразу: если initData не передан ИЛИ не прошёл
    # проверку подписи, ниже мы всё равно не должны упасть в UnboundLocalError.
    me = None
    my_status, my_bet, my_cashout, my_win = "none", 0, 0, 0
    if initData:
        u = verify_telegram_data(initData)
        if u:
            uid = str(u["id"])
            me = next((p for p in crash_state["players"] if str(p["id"]) == uid), None)
            if me:
                my_bet = me["bet"]
                if crash_state["status"] == "crashed" and me["cashout_mult"] == 0:
                    my_status = "lost"
                elif me["cashout_mult"] > 0:
                    my_status = "cashed_out"
                    my_cashout = me["cashout_mult"]
                    my_win = int(me["bet"] * me["cashout_mult"])
                else:
                    my_status = "bet"

    players_sorted = sorted(crash_state["players"], key=lambda p: p["bet"], reverse=True)

    crash_point_to_send = 0
    if crash_state["status"] == "crashed":
        crash_point_to_send = round(crash_state["crash_point"], 2)

    return {
        "status": crash_state["status"], "timer": crash_state["timer"],
        "multiplier": crash_state["multiplier"],
        "crash_point": crash_point_to_send,
        "bank": crash_state["bank"], "round_id": crash_state["round_id"],
        "players": players_sorted, "history": _refresh_crash_history()[:CRASH_HISTORY_LIMIT],
        "my_status": my_status, "my_bet": my_bet, "my_cashout_mult": my_cashout, "my_win": my_win,
        "status_start": crash_state["status_start"],
    }


@app.post("/api/crash_bet")
async def crash_bet(data: CrashBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])

    check_ban(user_id)

    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше нуля")

    if crash_state["status"] != "waiting": raise HTTPException(status_code=400,
                                                               detail="Ставки принимаются только в фазе ожидания")

    existing = next((p for p in crash_state["players"] if str(p["id"]) == user_id), None)
    # Лимит по сумме всех ставок игрока в раунде (докрутки тоже считаются).
    _enforce_bet_limit(user_id, int(existing["bet"] if existing else 0) + int(data.amount))
    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Недостаточно коинов")

    updated = supabase.table("users").update({"coins": current_coins - data.amount}).eq(
        "user_id", int(user_id)).select("user_id").execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Не удалось списать ставку")
    if existing:
        existing["bet"] += data.amount
    else:
        name = user.get("username", f"id{user_id}")
        avatar = user.get("photo_url", "")
        crash_state["players"].append(
            {"id": user_id, "name": name, "bet": data.amount, "cashout_mult": 0, "is_bot": False, "avatar": avatar,
             "auto_mult": 0})
    crash_state["bank"] += data.amount
    return {"success": True, "new_balance": current_coins - data.amount}


@app.post("/api/crash_cashout")
async def crash_cashout(data: CrashCashout):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
    check_ban(user_id)

    if crash_state["status"] != "flying": raise HTTPException(status_code=400, detail="Игра не идёт")
    player = next((p for p in crash_state["players"] if str(p["id"]) == user_id), None)
    if not player: raise HTTPException(status_code=400, detail="Вы не участвуете")
    if player["cashout_mult"] > 0: raise HTTPException(status_code=400, detail="Вы уже забрали")

    cashout_mult = crash_state["multiplier"]
    win_amount = int(player["bet"] * cashout_mult)
    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    new_coins = int(res.data[0].get("coins", 0)) + win_amount
    updated = supabase.table("users").update({"coins": new_coins}).eq(
        "user_id", int(user_id)).select("user_id").execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Не удалось зачислить выигрыш")
    player["cashout_mult"] = cashout_mult
    return {"success": True, "win_amount": win_amount, "mult": cashout_mult, "new_balance": new_coins}


@app.get("/api/crash_history")
async def get_crash_history(): return _refresh_crash_history(force=True)


# ==================== МИНЫ ====================
# Поле и множители считает сервер: клиент только рисует ответ, поэтому
# подкрутить «безопасную» клетку или выплату из браузера больше нельзя.
mines_games = {}


def _mines_multiplier(mines_count, opened):
    table = MINES_MULTIPLIERS.get(int(mines_count)) or MINES_MULTIPLIERS[1]
    if opened <= 0:
        return 1.0
    return table[min(opened, len(table)) - 1]


def _mines_first_click_chance(bet, mines_count):
    """Честный шанс (мины/16) плюс надбавка за крупную ставку и за 5 мин."""
    chance = min(1.0, int(mines_count) / MINES_CELLS)
    if int(bet) > MINES_RISKY_BET:
        chance += MINES_RISKY_BET_BONUS
    if int(mines_count) >= MINES_MAX_COUNT:
        chance += MINES_FIVE_MINES_BONUS
    return min(MINES_MAX_FIRST_CHANCE, chance)


def _mines_cashout_value(game):
    return int(game["bet"] * _mines_multiplier(game["mines"], len(game["opened"])))


def _mines_game_active(user_id):
    game = mines_games.get(str(user_id))
    return game if game and game.get("status") == "active" else None


@app.post("/api/mines_start")
async def mines_start(data: MinesStart):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
    check_ban(user_id)

    try:
        amount = int(data.amount)
        mines_count = int(data.mines)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная ставка")
    if amount < MINES_MIN_BET:
        raise HTTPException(status_code=400, detail=f"Минимальная ставка {MINES_MIN_BET} 🪙")
    if mines_count < 1 or mines_count > MINES_MAX_COUNT:
        raise HTTPException(status_code=400, detail="Можно выбрать от 1 до 5 мин")
    _enforce_bet_limit(user_id, amount)

    row = _db_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    coins = int(row.get("coins", 0) or 0)

    # Незавершённая игра не должна «съедать» ставку: забираем её по текущему кэфу.
    previous = _mines_game_active(user_id)
    if previous:
        coins += _mines_cashout_value(previous)
        mines_games.pop(user_id, None)

    if coins < amount:
        raise HTTPException(status_code=400, detail="Недостаточно коинов")
    coins -= amount
    if not _db_update_user(user_id, coins=coins):
        raise HTTPException(status_code=500, detail="Не удалось списать ставку")

    mines_games[user_id] = {
        "bet": amount, "mines": mines_count, "positions": None, "opened": [],
        "first_click_chance": _mines_first_click_chance(amount, mines_count),
        "started_at": time.time(), "status": "active",
    }
    return {"success": True, "new_balance": coins, "coef": 1.0, "cashout": amount,
            "mines": mines_count, "bet": amount}


@app.post("/api/mines_open")
async def mines_open(data: MinesOpen):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
    check_ban(user_id)

    game = _mines_game_active(user_id)
    if not game:
        raise HTTPException(status_code=400, detail="Игра не найдена — сделайте ставку")

    try:
        cell = int(data.cell)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная клетка")
    if cell < 0 or cell >= MINES_CELLS:
        raise HTTPException(status_code=400, detail="Некорректная клетка")
    if cell in game["opened"]:
        raise HTTPException(status_code=400, detail="Клетка уже открыта")

    if game["positions"] is None:
        # Позиции мин рождаются на первом клике: так работает надбавка к риску.
        positions = set()
        if random.random() < game["first_click_chance"]:
            positions.add(cell)
        need = game["mines"] - len(positions)
        pool = [index for index in range(MINES_CELLS) if index not in positions]
        positions.update(random.sample(pool, need))
        game["positions"] = sorted(positions)

    if cell in game["positions"]:
        game["status"] = "lost"
        mines_games.pop(user_id, None)
        return {"success": True, "hit": True, "positions": list(game["positions"]),
                "bet": game["bet"], "mines": game["mines"]}

    game["opened"].append(cell)
    opened_count = len(game["opened"])
    coef = _mines_multiplier(game["mines"], opened_count)
    return {
        "success": True, "hit": False, "cell": cell,
        "opened": list(game["opened"]), "opened_count": opened_count,
        "coef": round(coef, 2), "cashout": int(game["bet"] * coef),
        "bet": game["bet"], "mines": game["mines"],
    }


@app.post("/api/mines_cashout")
async def mines_cashout(data: MinesCashout):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
    check_ban(user_id)

    # Снимаем игру из памяти ДО начисления: повторный клик по «Забрать»
    # получит 400 и не сможет зачислить выигрыш второй раз.
    game = mines_games.pop(user_id, None)
    if not game or game.get("status") != "active":
        raise HTTPException(status_code=400, detail="Игра не найдена — сделайте ставку")

    game["status"] = "cashed"
    win_amount = _mines_cashout_value(game)
    row = _db_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    coins = int(row.get("coins", 0) or 0) + win_amount
    if not _db_update_user(user_id, coins=coins):
        raise HTTPException(status_code=500, detail="Не удалось зачислить выигрыш")
    return {"success": True, "win_amount": win_amount, "new_balance": coins,
            "coef": round(_mines_multiplier(game["mines"], len(game["opened"])), 2)}


# ==================== ЮЗЕР И ЭКОНОМИКА ====================
@app.post("/api/user")
async def get_user(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    raw_id = user.get("id")
    user_id = _safe_int(raw_id, None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")

    try:
        res_ban = supabase.table("bans").select("*").eq("user_id", user_id).execute()
        banned = bool(res_ban.data)
    except Exception:
        logger.exception("Не удалось проверить бан user_id=%s", user_id)
        banned = False
    if banned:
        raise HTTPException(status_code=403, detail="Вы забанены за багоюз или нарушение правил.")

    is_admin = is_admin_id(user_id, ADMIN_IDS)

    try:
        rows = _find_user_rows(user_id)
    except Exception:
        logger.exception("Ошибка чтения профиля user_id=%s", user_id)
        raise HTTPException(status_code=503, detail="База временно недоступна, попробуйте ещё раз")

    if not rows:
        # Создаём профиль только если строки действительно нет ни в одном варианте id.
        new_user = {"user_id": user_id, "coins": 0, "keys": 0,
                    "username": user.get("username", f"id{user_id}"),
                    "last_chest": 0, "chat_id": 0, "inventory": "[]"}
        try:
            supabase.table("users").insert(new_user).execute()
        except Exception:
            logger.exception("Не удалось создать профиль user_id=%s", user_id)
        rows = _find_user_rows(user_id)
        if not rows:
            raise HTTPException(status_code=503, detail="Профиль не найден в базе, попробуйте позже")

    # Если в базе оказалось несколько строк (старые дубли), берём ту, где больше коинов —
    # иначе игрок видит 0, хотя в «кармане» бота лежат его деньги.
    row = max(rows, key=lambda item: _safe_int(item.get("coins"), 0))
    if len(rows) > 1:
        logger.warning("Дубли профиля user_id=%s: %s", user_id,
                       [(_safe_int(item.get("id"), None), _safe_int(item.get("coins"), 0)) for item in rows])
    return _user_payload(row, is_admin)


@app.post("/api/self_check")
async def self_check(auth: UserAuth):
    """Диагностика для игрока: что именно видит сервер по его профилю.

    Помогает отличить три ситуации: initData не проходит проверку, строки нет
    в базе, или в базе лежит другая (нулевая) строка.
    """
    user = verify_telegram_data(auth.initData)
    if not user:
        return {"ok": False, "stage": "verify", "reason": "initData не прошёл проверку подписи",
                "bot_token_set": bool(BOT_TOKEN)}

    user_id = _safe_int(user.get("id"), None)
    result = {
        "ok": True, "stage": "row", "user_id": user_id,
        "username": user.get("username") or user.get("first_name") or "",
        "bot_token_set": bool(BOT_TOKEN),
        "supabase_host": _supabase_host(),
        "is_admin": is_admin_id(user_id, ADMIN_IDS) if user_id is not None else False,
        "rows_found": 0, "row": None, "created_at_present": None, "error": None,
    }
    if user_id is None:
        result.update({"ok": False, "reason": "в initData нет числового id"})
        return result

    try:
        rows = _find_user_rows(user_id)
    except Exception as exc:  # noqa: BLE001
        result.update({"ok": False, "stage": "query", "reason": "ошибка запроса к базе",
                       "error": f"{type(exc).__name__}: {exc}"[:300]})
        return result

    result["rows_found"] = len(rows)
    if not rows:
        result.update({"ok": False, "reason": "строки с таким user_id нет в таблице users"})
        return result

    row = max(rows, key=lambda item: _safe_int(item.get("coins"), 0))
    result["created_at_present"] = "created_at" in row
    result["row"] = {
        "id": _safe_int(row.get("id"), None),
        "user_id_raw": str(row.get("user_id")),
        "coins_raw": repr(row.get("coins")),
        "coins": _safe_int(row.get("coins"), 0),
        "keys_raw": repr(row.get("keys")),
        "last_chest_raw": repr(row.get("last_chest")),
        "inventory_type": type(row.get("inventory")).__name__,
    }
    result["profile"] = _user_payload(row, result["is_admin"])
    if result["is_admin"]:
        # Готовность лога апгрейдов: по нему видно, применена ли миграция.
        try:
            supabase.table("upgrade_log").select("id").limit(1).execute()
            result["upgrade_log_ready"] = True
        except Exception as exc:  # noqa: BLE001
            result["upgrade_log_ready"] = False
            result["upgrade_log_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return result


def _supabase_host():
    url = os.getenv("SUPABASE_URL") or ""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or ""
    except Exception:
        return ""


@app.post("/api/open_chest")
async def open_chest(auth: OpenChestRequest):
    """Сундук открывает и награждает сервер: обычный 10-30 коинов или 20% на ключ."""
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
    check_ban(user_id)

    chest_type = (auth.chest_type or "normal").strip().lower()
    if chest_type not in ("normal", "gold"):
        raise HTTPException(status_code=400, detail="Неизвестный тип сундука")

    now = int(time.time())
    row = _db_user(user_id)
    if row is None: raise HTTPException(status_code=404, detail="User not found")

    coins = int(row.get("coins", 0) or 0)
    keys = int(row.get("keys", 0) or 0)
    last_chest = int(row.get("last_chest", 0) or 0)

    if chest_type == "gold":
        if keys <= 0:
            raise HTTPException(status_code=400, detail="У вас нет ключей!")
        keys -= 1
        reward_coins = 0 if random.random() < GOLD_KEY_BACK_CHANCE else random.randint(GOLD_MIN_COINS, GOLD_MAX_COINS)
        reward_keys = 1 if reward_coins == 0 else 0
    else:
        if (now - last_chest) < CHEST_COOLDOWN:
            raise HTTPException(status_code=400, detail="Сундук перезаряжается")
        last_chest = now
        if random.random() < CHEST_KEY_CHANCE:
            reward_coins, reward_keys = 0, 1
        else:
            reward_coins, reward_keys = random.randint(CHEST_MIN_COINS, CHEST_MAX_COINS), 0

    coins += reward_coins
    keys += reward_keys
    if not _db_update_user(user_id, coins=coins, keys=keys, last_chest=last_chest):
        raise HTTPException(status_code=500, detail="Не удалось открыть сундук")

    return {
        "success": True, "chest_type": chest_type,
        "reward_coins": reward_coins, "reward_keys": reward_keys,
        "new_balance": coins, "new_keys": keys, "last_chest": last_chest,
    }


@app.post("/api/shop_items")
async def get_shop_items(data: ShopCatalogRequest):
    """Shop catalog for the current user. Admin-only capsules are never included for regular users."""
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = int(user["id"])
    check_ban(str(user_id))
    is_admin = is_admin_id(user_id, ADMIN_IDS)
    return {"is_admin": is_admin, "items": serialize_shop_items(is_admin)}


@app.post("/api/buy_item")
async def buy_item(data: BuyItem):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = int(user["id"])
    check_ban(str(user_id))
    if data.item_id not in SHOP_ITEMS:
        raise HTTPException(status_code=400, detail="Неизвестный предмет магазина")

    try:
        return purchase_shop_item(supabase, user_id, data.item_id, ADMIN_IDS)
    except ShopPurchaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

# ==================== АПГРЕЙД (TONGRADER) ====================
def _upgrade_normalize_request(data: "UpgradeAction") -> tuple[str, int, str, str]:
    """Приводим новый и старый контракт к (source, amount, target, extra).

    Новый клиент присылает item_from / amount_from / item_to (+ target_*),
    старый закешированный — bet_type / amount|quantity / target_multiplier.
    """
    source = (data.item_from or data.bet_type or "").strip().lower()
    if source not in UPGRADE_SOURCES:
        raise HTTPException(status_code=400, detail="Неизвестный предмет для апгрейда")

    raw_amount = data.amount_from if data.amount_from is not None else (
        data.amount if data.amount is not None else data.quantity)
    try:
        amount = int(raw_amount or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректное количество")

    target = (data.item_to or "").strip().lower()
    if not target:
        # старый контракт: bet_type=capsule подразумевал апгрейд в уникальную капсулу
        target = "uniq_capsule" if source == "capsule" else "coin"

    if target not in UPGRADE_TARGETS:
        raise HTTPException(status_code=400, detail="Неизвестная цель апгрейда")
    return source, amount, target, source


def _upgrade_source_value(source: str, amount: int) -> int:
    if source == "coin":
        return amount
    return UPGRADE_ITEM_PRICES[source] * amount


def _upgrade_target_value(source: str, amount: int, target: str,
                          multiplier: Optional[float], chance: Optional[float]) -> tuple[int, float]:
    """Возвращает (стоимость цели, визуальный шанс успеха в процентах)."""
    from_value = _upgrade_source_value(source, amount)

    if target == "coin":
        if chance is not None:
            if chance <= 0 or chance > UPGRADE_MAX_CHANCE:
                raise HTTPException(status_code=400, detail=f"Шанс должен быть от 0 до {int(UPGRADE_MAX_CHANCE)}%")
            to_value = int(round(from_value / (chance / 100.0)))
        else:
            try:
                multiplier = float(multiplier or 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Некорректный коэффициент")
            if multiplier <= 1:
                raise HTTPException(status_code=400, detail="Коэффициент должен быть больше x1")
            to_value = int(round(from_value * multiplier))
        win_amount = to_value
    else:
        # Цель — капсула: за успех выдаём ровно одну штуку.
        to_value = UPGRADE_ITEM_PRICES[target]
        win_amount = 1

    if to_value <= 0:
        raise HTTPException(status_code=400, detail="Некорректная цель апгрейда")
    if to_value > UPGRADE_MAX_REWARD:
        raise HTTPException(status_code=400, detail="Слишком большой апгрейд за один раз")

    visual_chance = from_value / to_value * 100.0
    if visual_chance > UPGRADE_MAX_CHANCE:
        raise HTTPException(status_code=400,
                            detail=f"Шанс {visual_chance:.2f}% слишком высокий — выбери цель подороже")
    if visual_chance < UPGRADE_MIN_CHANCE:
        raise HTTPException(status_code=400,
                            detail=f"Шанс {visual_chance:.2f}% слишком низкий — выбери цель подешевле")
    return win_amount, visual_chance


def _upgrade_read_user(user_id: int):
    """Читаем только нужные поля — никаких обращений к несуществующим колонкам (PGRST204)."""
    response = (
        supabase.table("users")
        .select("coins, inventory")
        .eq("user_id", user_id)
        .execute()
    )
    if not response.data:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    row = response.data[0]
    raw_coins = row.get("coins") or 0
    try:
        coins = int(raw_coins)
    except (TypeError, ValueError):
        coins = 0
    return coins, row.get("inventory")


def _pg_filter_safe(value) -> bool:
    """PostgREST ломает запрос, если в значении есть , : ( ) или кавычки.

    Инвентарь — это JSON-строка вида ["capsule","capsule"]: запятые и кавычки там
    обычное дело. Раньше такой фильтр уходил как есть, PostgREST не мог его
    разобрать, и апгрейд падал с 500. Поэтому сверяем инвентарь только когда
    значение безопасно передать (чаще всего — один предмет)."""
    if not isinstance(value, str):
        return False
    return not any(char in value for char in ',:()"')


def _upgrade_write_user(user_id: int, raw_coins: int, raw_inventory, coins: int, inventory: list) -> bool:
    """Оптимистичная запись: применяется, только если баланс (и по возможности
    инвентарь) не изменились с момента чтения. Ошибка сети/PostgREST — это
    False, а не 500: выше стоит повторное чтение и ещё одна попытка."""
    update_values = {"coins": coins, "inventory": json.dumps(inventory, ensure_ascii=False)}
    query = supabase.table("users").update(update_values).eq("user_id", user_id).eq("coins", raw_coins)
    if raw_inventory is not None and _pg_filter_safe(raw_inventory):
        query = query.eq("inventory", raw_inventory)
    try:
        updated = query.select("user_id").execute()
    except Exception:
        logger.warning("Не удалось записать результат апгрейда", exc_info=True)
        return False
    return bool(updated.data)


# ==================== ЛОГ АПГРЕЙДОВ ====================
# Каждый розыгрыш пишется в upgrade_log: что ставили, какой был шанс, что выпало,
# сколько стало коинов и капсул. Это и доказательство выплаты, и честная статистика
# («Апгрейдов / Успешных / Лучший»), и восстановление результата, если игрок
# закрыл аппку до конца анимации.
UPGRADE_LOG_LIMIT = 1000


def _upgrade_log_rows(user_id, limit: int = UPGRADE_LOG_LIMIT):
    """None — таблицы нет (миграция не применена) или база недоступна."""
    try:
        res = (supabase.table("upgrade_log")
               .select("*")
               .eq("user_id", int(user_id))
               .order("id", desc=True)
               .limit(limit)
               .execute())
    except Exception:
        logger.warning("upgrade_log недоступен — примените миграцию 20261029000000_upgrade_log.sql")
        return None
    return res.data or []


def _upgrade_reward_value(row) -> int:
    """Ценность приза в коинах — так «Лучший выигрыш» сравним для коинов и капсул."""
    amount = _safe_int(row.get("win_amount"), 0)
    win_item = (row.get("win_item") or "").strip()
    if not win_item:
        return 0
    if win_item == "coin":
        return amount
    return UPGRADE_ITEM_PRICES.get(win_item, 0) * amount


def _upgrade_stats_from_rows(rows) -> dict:
    wins = sum(1 for row in rows if row.get("won"))
    best = 0
    for row in rows:
        if row.get("won"):
            best = max(best, _upgrade_reward_value(row))
    return {"total": len(rows), "wins": wins, "best": best}


def _upgrade_log_public(row) -> dict:
    if not row:
        return None
    created = row.get("created_at")
    return {
        "id": _safe_int(row.get("id"), None),
        "item_from": row.get("item_from"),
        "amount_from": _safe_int(row.get("amount_from"), 0),
        "item_to": row.get("item_to"),
        "chance": round(float(row.get("chance") or 0), 2),
        "roll": round(float(row.get("roll") or 0), 2),
        "won": bool(row.get("won")),
        "win_item": row.get("win_item"),
        "win_amount": _safe_int(row.get("win_amount"), 0),
        "coins_after": _safe_int(row.get("coins_after"), 0),
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "seen": bool(row.get("seen")),
    }


def _upgrade_log_write(user_id, **fields):
    """Пишем розыгрыш best-effort: без миграции игра обязана продолжать работать.
    Возвращаем id записи (если база его отдала) или None."""
    payload = {
        "user_id": int(user_id),
        "item_from": fields.get("item_from"),
        "amount_from": _safe_int(fields.get("amount_from"), 0),
        "item_to": fields.get("item_to"),
        "chance": round(float(fields.get("chance") or 0), 2),
        "roll": round(float(fields.get("roll") or 0), 2),
        "won": bool(fields.get("won")),
        "win_item": fields.get("win_item"),
        "win_amount": _safe_int(fields.get("win_amount"), 0),
        "coins_before": _safe_int(fields.get("coins_before"), 0),
        "coins_after": _safe_int(fields.get("coins_after"), 0),
        # Именно список, а не JSON-строка: колонка jsonb должна хранить массив.
        "inventory_after": list(fields.get("inventory_after") or []),
        "seen": False,
    }
    try:
        res = supabase.table("upgrade_log").insert(payload).execute()
        rows = getattr(res, "data", None) or []
        if rows and isinstance(rows[0], dict):
            return _safe_int(rows[0].get("id"), None)
        return None
    except Exception:
        logger.warning("Не удалось записать апгрейд в upgrade_log (миграция не применена?)")
        return None


@app.post("/api/upgrade_action")
async def upgrade_action(data: UpgradeAction):
    """Апгрейд предмета: слева ставка, справа цель. Шанс = стоимость слева / стоимость справа."""
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")

    user_id = int(user["id"])
    admin = is_admin_id(user_id, ADMIN_IDS)
    if not admin:
        # Игра пока открыта только администраторам.
        raise HTTPException(status_code=403, detail="Апгрейд доступен только администраторам")

    check_ban(str(user_id))

    source, amount, target, _ = _upgrade_normalize_request(data)

    if source == "coin":
        if amount < UPGRADE_MIN_BET:
            raise HTTPException(status_code=400, detail=f"Минимальная ставка {UPGRADE_MIN_BET} 🪙")
        if amount > UPGRADE_MAX_BET:
            raise HTTPException(status_code=400, detail=f"Максимальная ставка {UPGRADE_MAX_BET} 🪙")
    else:
        if amount < 1:
            raise HTTPException(status_code=400, detail="Минимум 1 капсула")
        if amount > UPGRADE_MAX_CAPSULES:
            raise HTTPException(status_code=400, detail=f"Максимум {UPGRADE_MAX_CAPSULES} капсул за раз")

    win_amount, visual_chance = _upgrade_target_value(
        source, amount, target, data.target_multiplier, data.target_chance
    )
    # Пока играют только админы — шанс честный. Для обычных игроков (в будущем)
    # реальный шанс будет в UPGRADE_NON_ADMIN_DIVIDER раз меньше визуального.
    real_chance = visual_chance if admin else visual_chance / UPGRADE_NON_ADMIN_DIVIDER

    for _attempt in range(5):
        raw_coins, raw_inventory = _upgrade_read_user(user_id)
        inventory = parse_inventory(raw_inventory)

        # Списание сразу — до броска, чтобы ставку нельзя было абузить.
        if source == "coin":
            if raw_coins < amount:
                raise HTTPException(status_code=400, detail="Недостаточно коинов")
            new_coins = raw_coins - amount
        else:
            owned = inventory.count(source)
            if owned < amount:
                raise HTTPException(status_code=400, detail="Недостаточно капсул")
            for _ in range(amount):
                inventory.remove(source)
            new_coins = raw_coins

        # Фиксируем сам бросок: его видно в логе, поэтому «не выдалось» проверяемо.
        roll = round(random.random() * 100.0, 2)
        won = roll < real_chance
        win_item = None
        # payout — только для этого круга. Раньше здесь обнулялся сам win_amount:
        # если запись не проходила из-за гонки, следующий круг мог выиграть с
        # payout = 0 — «приз не выдался».
        payout = 0
        if won:
            new_inventory = list(inventory)
            if target == "coin":
                payout = win_amount
                new_coins += payout
                win_item = "coin"
            else:
                new_inventory.append(target)
                win_item = target
                payout = 1
            # Предохранитель по одной выплате, а не по всему балансу: раньше здесь
            # стоял min(new_coins, UPGRADE_MAX_REWARD * 10), и игрок с балансом
            # больше миллиона терял всё сверху — «приз не выдался».
            new_coins = min(new_coins, raw_coins + UPGRADE_MAX_REWARD)
        else:
            new_inventory = list(inventory)

        if _upgrade_write_user(user_id, raw_coins, raw_inventory, new_coins, new_inventory):
            # Лог пишем ПОСЛЕ успешной записи баланса: если она не прошла, розыгрыша не было.
            log_id = _upgrade_log_write(
                user_id, item_from=source, amount_from=amount, item_to=target,
                chance=visual_chance, roll=roll, won=won, win_item=win_item,
                win_amount=payout,
                coins_before=raw_coins, coins_after=new_coins, inventory_after=new_inventory,
            )
            return {
                "success": won,
                "won": won,
                "item_from": source,
                "amount_from": amount,
                "item_to": target,
                "chance": round(visual_chance, 2),
                "roll": roll,
                "log_id": log_id,
                "win_item": win_item,
                "win_amount": payout,
                "new_balance": new_coins,
                "inventory": new_inventory,
                "new_inventory": new_inventory,
            }

    raise HTTPException(status_code=409, detail="Апгрейд уже обрабатывается. Попробуйте ещё раз.")


@app.post("/api/upgrade_history")
async def upgrade_history(data: UpgradeLogRequest):
    """Честная статистика апгрейдов + последний непоказанный результат.

    Нужен, чтобы: 1) счётчик «Апгрейдов / Успешных / Лучший» не сбрасывался,
    2) игрок увидел приз, даже если закрыл аппку до конца анимации.
    """
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = _safe_int(user.get("id"), None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    check_ban(str(user_id))
    if not is_admin_id(user_id, ADMIN_IDS):
        raise HTTPException(status_code=403, detail="Апгрейд доступен только администраторам")

    rows = _upgrade_log_rows(user_id)
    if rows is None:
        return {"available": False, "stats": None, "last_unseen": None,
                "detail": "Таблица upgrade_log ещё не создана — примените миграцию"}
    last_unseen = next((row for row in rows if not row.get("seen")), None)
    return {
        "available": True,
        "stats": _upgrade_stats_from_rows(rows),
        "last_unseen": _upgrade_log_public(last_unseen),
        "history": [_upgrade_log_public(row) for row in rows[:10]],
    }


@app.post("/api/upgrade_ack")
async def upgrade_ack(data: UpgradeAck):
    """Помечаем результаты просмотренными, чтобы не показывать их повторно."""
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = _safe_int(user.get("id"), None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    check_ban(str(user_id))
    if not is_admin_id(user_id, ADMIN_IDS):
        raise HTTPException(status_code=403, detail="Апгрейд доступен только администраторам")

    try:
        query = supabase.table("upgrade_log").update({"seen": True}).eq("user_id", user_id)
        if data.log_id is not None:
            query = query.lte("id", int(data.log_id))
        query.execute()
    except Exception:
        logger.warning("Не удалось отметить апгрейд просмотренным (миграция не применена?)")
        return {"success": False, "available": False}
    return {"success": True, "available": True}


@app.post("/api/update_balance")
async def update_balance(data: UpdateBalance):
    """Служебный эндпоинт. Раньше любой клиент мог начислить себе сколько угодно
    коинов — теперь все награды начисляет сервер, а прямой вызов закрыт."""
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    check_ban(user_id)
    if not is_admin_id(int(user_id), ADMIN_IDS):
        raise HTTPException(status_code=403, detail="Операция недоступна")

    res = supabase.table("users").select("coins").eq("user_id", user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0) or 0)
    new_coins = max(0, current_coins + data.amount)
    if not _db_update_user(user_id, coins=new_coins):
        raise HTTPException(status_code=500, detail="Не удалось обновить баланс")
    return {"success": True, "new_balance": new_coins}


@app.post("/api/update_keys")
async def update_keys(data: UpdateKeys):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    check_ban(user_id)
    if not is_admin_id(int(user_id), ADMIN_IDS):
        raise HTTPException(status_code=403, detail="Операция недоступна")

    res = supabase.table("users").select("keys").eq("user_id", user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_keys = int(res.data[0].get("keys", 0) or 0)
    new_keys = max(0, current_keys + data.amount)
    if not _db_update_user(user_id, keys=new_keys):
        raise HTTPException(status_code=500, detail="Не удалось обновить ключи")
    return {"success": True, "new_keys": new_keys}


@app.get("/api/global_top")
async def get_global_top():
    res = supabase.table("users").select("user_id, username, coins").order("coins", desc=True).limit(50).execute()
    filtered = [u for u in res.data if int(u["user_id"]) not in ADMIN_IDS]
    return filtered[:50]


@app.get("/api/season_end")
async def get_season_end():
    end_date = datetime.datetime(2026, 11, 1, 15, 0, 0)
    return {"end_timestamp": int(end_date.timestamp())}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))