from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import os, hashlib, hmac, json, time, random, uvicorn, datetime, math
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])

if os.path.exists("miniapp"):
    app.mount("/miniapp", StaticFiles(directory="miniapp", html=True), name="miniapp")

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = [6338018270, 5184813347, 8890834384]
MAX_BET = 10000

BOT_NAMES = ["Shark_V", "LuckyFox", "NeonWolf", "GhostByte", "PixelKing", "CyberCat", "IronFist", "VoidWalker",
             "ShadowAce", "RogueOne", "Toxic", "Viper", "Sniper", "ProGamer", "NoobMaster", "Killer", "Dragon"]
BOT_COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#5856d6", "#af52de", "#ff2d55"]
WHALE_NAMES = ["KingPin", "MrMoney", "WhaleGod", "BigBoss", "RichMan", "GoldFinger"]
WHALE_COLORS = ["#ffd700", "#c0c0c0", "#b8860b", "#ff8c00", "#8b0000", "#daa520"]


def _random_arena_bot_count():
    # Keep most rounds light; large bot rooms are occasional.
    return random.randint(1, 3) if random.random() < 0.85 else random.randint(4, 10)


# === АРЕНА ===
game_state = {
    "status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
    "last_update": time.time(), "last_bot_added": time.time(), "target_bots": _random_arena_bot_count(), "is_paid": False,
    "round_id": str(int(time.time())), "whales_added": 0,
    "roll_data": None, "rolling_started_at": None, "whale_decided": False,
    "whale_join": False, "whale_trigger_at": None
}
history = []


HOCKEY_ROLL_DURATION = 9.0
HOCKEY_WAIT_DURATION = 15.0
HOCKEY_ROUND_PERIOD = HOCKEY_WAIT_DURATION + HOCKEY_ROLL_DURATION + 5.0
HOCKEY_SIMULATION_STEP = 0.05
HOCKEY_DAMPING = 0.985
HOCKEY_STOP_DAMPING = 0.90
HOCKEY_SLOWDOWN_START = 6.0


def _simulate_hockey_roll(start_angle, speed, start_x=50.0, start_y=50.0):
    """Precompute the authoritative path once; clients only render these server points."""
    x, y = float(start_x), float(start_y)
    vx = math.cos(start_angle) * speed
    vy = math.sin(start_angle) * speed
    steps = round(HOCKEY_ROLL_DURATION / HOCKEY_SIMULATION_STEP)
    path = [[round(x, 6), round(y, 6)]]

    slowdown_step = round(HOCKEY_SLOWDOWN_START / HOCKEY_SIMULATION_STEP)
    for step in range(steps):
        x += vx
        y += vy
        if x <= 0 or x >= 100:
            vx *= -1
            x = max(0.0, min(100.0, x))
        if y <= 0 or y >= 100:
            vy *= -1
            y = max(0.0, min(100.0, y))
        damping = HOCKEY_STOP_DAMPING if step >= slowdown_step else HOCKEY_DAMPING
        vx *= damping
        vy *= damping
        path.append([round(x, 6), round(y, 6)])

    return path


def _prepare_hockey_roll(now):
    """Create a deterministic-per-round path shared even across API workers."""
    # All workers use the same time-slot round id and RNG seed, rather than process-local randomness.
    round_slot = int(now // HOCKEY_ROUND_PERIOD)
    game_state["round_id"] = str(round_slot)
    round_rng = random.Random(round_slot)
    start_angle = round_rng.uniform(0.0, 2 * math.pi)
    speed = 8.5
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
    """Credit only the real winner once; bots and clients never receive or trigger payouts."""
    winner = game_state["winner"]
    if game_state["is_paid"] or not winner:
        return
    if winner.get("is_bot"):
        game_state["is_paid"] = True
        return

    # Mark first so simultaneous state requests cannot credit the same round twice.
    game_state["is_paid"] = True
    winner_id = game_state["winner"]["id"]
    try:
        numeric_winner_id = int(winner_id)
        res = supabase.table("users").select("coins").eq("user_id", numeric_winner_id).execute()
        if res.data:
            new_coins = int(res.data[0].get("coins", 0)) + int(game_state["bank"])
            supabase.table("users").update({"coins": new_coins}).eq("user_id", numeric_winner_id).execute()
    except (TypeError, ValueError):
        # Arena bots do not have user accounts and must never receive coins.
        pass
    except Exception as e:
        print(f"❌ Ошибка выплаты Арены: {e}")


def _finish_hockey_round():
    """Resolve from the server-simulated final x and pay exactly once."""
    if game_state["status"] != "rolling":
        return False
    # Virtual bot bets affect sector odds only; the payout pot contains charged human stakes.
    game_state["bank"] = sum(int(player["bet"]) for player in game_state["players"] if not player.get("is_bot"))
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
crash_history = []


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


class BuyItem(BaseModel): initData: str; item_id: str; price: int


class PlaceBet(BaseModel): initData: str; amount: int


class FinishHockey(BaseModel): initData: str; x: float; y: float


class CrashBet(BaseModel): initData: str; amount: int


class CrashCashout(BaseModel): initData: str


# ==================== АРЕНА (ХОККЕЙ) ====================
def _hockey_state_response():
    response = game_state.copy()
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
            # Freeze the real pot; bot stakes only define their visible sector odds.
            game_state["bank"] = sum(int(player["bet"]) for player in game_state["players"] if not player.get("is_bot"))
            game_state["status"] = "rolling"
            game_state["last_update"] = now
            _update_hockey_percentages()
            _prepare_hockey_roll(now)

    elif game_state["status"] == "rolling":
        if now - game_state["last_update"] >= HOCKEY_ROLL_DURATION:
            # Fallback is server-authoritative: leaving the app cannot cancel the draw or payout.
            _finish_hockey_round()

    elif game_state["status"] == "finished":
        if now - game_state["last_update"] > 5:
            game_state = {"status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
                          "last_update": now, "last_bot_added": now, "target_bots": _random_arena_bot_count(),
                          "is_paid": False, "round_id": str(int(time.time())), "whales_added": 0,
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
    if int(user_id) not in ADMIN_IDS and data.amount > MAX_BET:
        raise HTTPException(status_code=400, detail=f"Максимальная ставка {MAX_BET} 🪙")
    if (game_state["status"] != "waiting"
            or time.time() - game_state["last_update"] >= 15):
        raise HTTPException(status_code=400, detail="Ставки закрыты")
    if any(p["id"] == user_id and not p.get("is_bot") for p in game_state["players"]):
        raise HTTPException(status_code=400, detail="Вы уже сделали ставку в этом раунде")
    if len(game_state["players"]) >= 20:
        raise HTTPException(status_code=400, detail="Комната заполнена")

    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    new_coins = current_coins - data.amount
    supabase.table("users").update({"coins": new_coins}).eq("user_id", int(user_id)).execute()

    name = user.get("username", user.get("first_name", f"id{user_id}"))
    avatar = user.get("photo_url", "")
    player_color = "#007aff"
    while any(p["color"] == player_color for p in game_state["players"]):
        player_color = f"hsl({random.randint(0, 360)}, 80%, 50%)"
    game_state["players"].append(
        {"id": user_id, "name": name, "avatar": avatar, "bet": data.amount, "color": player_color, "is_bot": False})

    # One 40% chance per round to add a whale shortly before closing, triggered by a 1,000+ human bet.
    if data.amount >= 1000 and not game_state["whale_decided"]:
        game_state["whale_decided"] = True
        game_state["whale_join"] = random.random() < 0.10
        if game_state["whale_join"]:
            game_state["whale_trigger_at"] = random.uniform(1.0, 2.0)

    # Only charged human stakes go into the payout pot. Bot bets affect sectors, not balances.
    game_state["bank"] = sum(int(player["bet"]) for player in game_state["players"] if not player.get("is_bot"))
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
    rv = random.random()
    if rv < 0.30:
        return random.uniform(1.00, 1.05)
    elif rv < 0.46:
        return random.uniform(1.05, 2.0)
    elif rv < 0.85:
        return random.uniform(2.0, 4.0)
    elif rv < 0.99:
        return random.uniform(4.0, 50.0)
    else:
        return 100.0


@app.get("/api/crash_state")
async def get_crash_state(initData: str = ""):
    global crash_state, crash_history
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

            crash_history.insert(0, {"round_id": crash_state["round_id"],
                                     "crash_point": round(crash_state["crash_point"], 2), "players": [
                    {"name": p["name"], "bet": p["bet"], "cashout": p["cashout_mult"], "is_bot": p["is_bot"]} for p in
                    crash_state["players"]]})
            if len(crash_history) > 20: crash_history.pop()

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
    me = None
    if initData:
        u = verify_telegram_data(initData)
        if u:
            uid = str(u["id"])
            me = next((p for p in crash_state["players"] if str(p["id"]) == uid), None)
            my_status, my_bet, my_cashout, my_win = "none", 0, 0, 0
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
    else:
        my_status, my_bet, my_cashout, my_win = "none", 0, 0, 0

    players_sorted = sorted(crash_state["players"], key=lambda p: p["bet"], reverse=True)

    crash_point_to_send = 0
    if crash_state["status"] == "crashed":
        crash_point_to_send = round(crash_state["crash_point"], 2)

    return {
        "status": crash_state["status"], "timer": crash_state["timer"],
        "multiplier": crash_state["multiplier"],
        "crash_point": crash_point_to_send,
        "bank": crash_state["bank"], "round_id": crash_state["round_id"],
        "players": players_sorted, "history": crash_history[:10],
        "my_status": my_status, "my_bet": my_bet, "my_cashout_mult": my_cashout, "my_win": my_win,
        "status_start": crash_state["status_start"],
    }


@app.post("/api/crash_bet")
async def crash_bet(data: CrashBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])

    check_ban(user_id)

    if int(user_id) not in ADMIN_IDS and data.amount > MAX_BET:
        raise HTTPException(status_code=400, detail=f"Максимальная ставка {MAX_BET} 🪙")

    if crash_state["status"] != "waiting": raise HTTPException(status_code=400,
                                                               detail="Ставки принимаются только в фазе ожидания")

    existing = next((p for p in crash_state["players"] if str(p["id"]) == user_id), None)
    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Недостаточно коинов")

    supabase.table("users").update({"coins": current_coins - data.amount}).eq("user_id", int(user_id)).execute()
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

    player["cashout_mult"] = crash_state["multiplier"]
    win_amount = int(player["bet"] * player["cashout_mult"])

    new_coins = 0
    try:
        res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
        current_coins = int(res.data[0].get("coins", 0)) if res.data else 0
        new_coins = current_coins + win_amount
        supabase.table("users").update({"coins": new_coins}).eq("user_id", int(user_id)).execute()
    except Exception as e:
        print(f"❌ cashout: {e}")
    return {"success": True, "win_amount": win_amount, "mult": player["cashout_mult"], "new_balance": new_coins}


@app.get("/api/crash_history")
async def get_crash_history(): return crash_history


# ==================== ЮЗЕР И ЭКОНОМИКА ====================
@app.post("/api/user")
async def get_user(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]

    res_ban = supabase.table("bans").select("*").eq("user_id", user_id).execute()
    if res_ban.data:
        raise HTTPException(status_code=403, detail="Вы забанены за багоюз или нарушение правил.")

    if user_id in ADMIN_IDS and user_id != user.get("id"): return {"coins": -1, "keys": 0, "inventory": [],
                                                                   "last_chest": 0, "hidden": True}
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if not res.data:
        new_user = {"user_id": user_id, "coins": 0, "keys": 0, "username": user.get("username", f"id{user_id}"),
                    "last_chest": 0, "chat_id": 0, "inventory": "[]"}
        supabase.table("users").insert(new_user).execute()
        return {"coins": 0, "keys": 0, "inventory": [], "last_chest": 0}
    db_user = res.data[0]
    return {"coins": int(db_user.get("coins", 0)), "keys": int(db_user.get("keys", 0)),
            "inventory": json.loads(db_user.get("inventory") or "[]"), "last_chest": int(db_user.get("last_chest", 0))}


@app.post("/api/open_chest")
async def open_chest(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    check_ban(user_id)

    now = int(time.time())
    res = supabase.table("users").select("last_chest").eq("user_id", user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    last_chest = int(res.data[0].get("last_chest", 0) or 0)
    if (now - last_chest) < 18000: raise HTTPException(status_code=400, detail="Сундук перезаряжается")
    supabase.table("users").update({"last_chest": now}).eq("user_id", user_id).execute()
    return {"success": True}


@app.post("/api/buy_item")
async def buy_item(data: BuyItem):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    check_ban(user_id)

    res = supabase.table("users").select("coins, inventory").eq("user_id", user_id).execute()
    current_coins = int(res.data[0].get("coins", 0))
    inventory = json.loads(res.data[0].get("inventory") or "[]")
    if current_coins < data.price: raise HTTPException(status_code=400, detail="Недостаточно коинов")
    inventory.append(data.item_id)
    supabase.table("users").update({"coins": current_coins - data.price, "inventory": json.dumps(inventory)}).eq(
        "user_id", user_id).execute()
    return {"success": True, "new_balance": current_coins - data.price, "inventory": inventory}


@app.post("/api/update_balance")
async def update_balance(data: UpdateBalance):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    check_ban(user_id)

    res = supabase.table("users").select("coins").eq("user_id", user_id).execute()
    current_coins = int(res.data[0].get("coins", 0))
    new_coins = max(0, current_coins + data.amount)
    supabase.table("users").update({"coins": new_coins}).eq("user_id", user_id).execute()
    return {"success": True, "new_balance": new_coins}


@app.post("/api/update_keys")
async def update_keys(data: UpdateKeys):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    check_ban(user_id)

    res = supabase.table("users").select("keys").eq("user_id", user_id).execute()
    current_keys = int(res.data[0].get("keys", 0) or 0)
    new_keys = max(0, current_keys + data.amount)
    supabase.table("users").update({"keys": new_keys}).eq("user_id", user_id).execute()
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