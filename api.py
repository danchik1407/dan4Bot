from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os, hashlib, hmac, json, time, random, uvicorn, datetime
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

BOT_NAMES = ["Shark_V", "LuckyFox", "NeonWolf", "GhostByte", "PixelKing", "CyberCat", "IronFist", "VoidWalker",
             "ShadowAce", "RogueOne", "Toxic", "Viper", "Sniper", "ProGamer", "NoobMaster", "Killer", "Dragon"]
BOT_COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#5856d6", "#af52de", "#ff2d55"]
WHALE_NAMES = ["KingPin", "MrMoney", "WhaleGod", "BigBoss", "RichMan", "GoldFinger"]
WHALE_COLORS = ["#ffd700", "#c0c0c0", "#b8860b", "#ff8c00", "#8b0000", "#daa520"]

# === ХОККЕЙ ===
game_state = {
    "status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
    "last_update": time.time(), "last_bot_added": 0, "target_bots": 0, "is_paid": False,
    "round_id": str(int(time.time())), "whales_added": 0, "whale_chance": random.random() < 0.07
}
history = []

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


# ==================== ХОККЕЙ ====================
@app.get("/api/hockey_state")
async def get_hockey_state():
    global game_state, history
    now = time.time()

    if game_state["status"] == "waiting":
        time_left = 15 - (now - game_state["last_update"])
        if time_left <= 10 and game_state["target_bots"] == 0:
            game_state["target_bots"] = random.randint(1, 5)
        if time_left <= 10 and game_state["target_bots"] > 0:
            current_bots = sum(1 for p in game_state["players"] if p["is_bot"] and p["bet"] < 500)
            current_whales = sum(1 for p in game_state["players"] if p["is_bot"] and p["bet"] >= 500)
            bot_delay = random.uniform(1.0, 4.0)

            if current_bots < game_state["target_bots"] and len(game_state["players"]) < 20 and (
                    now - game_state["last_bot_added"]) > bot_delay:
                bot_bet = random.randint(10, 100)
                bot_name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
                bot_color = random.choice(BOT_COLORS)
                while any(p["color"] == bot_color for p in game_state["players"]): bot_color = random.choice(BOT_COLORS)
                game_state["players"].append(
                    {"id": str(random.randint(10000, 99999)), "name": bot_name, "avatar": "", "bet": bot_bet,
                     "color": bot_color, "is_bot": True})
                game_state["bank"] += bot_bet
                game_state["last_bot_added"] = now
                game_state["target_bots"] = random.randint(1, 5)
                total_bank = game_state["bank"]
                if total_bank > 0:
                    for p in game_state["players"]: p["percent"] = (p["bet"] / total_bank) * 100

            if game_state["whale_chance"] and game_state["whales_added"] < 2 and current_whales < 2 and len(
                    game_state["players"]) < 20 and (now - game_state["last_bot_added"]) > bot_delay:
                if random.random() < 0.3:
                    whale_bet = random.randint(500, 1000)
                    whale_name = random.choice(WHALE_NAMES) + str(random.randint(1, 99))
                    whale_color = random.choice(WHALE_COLORS)
                    while any(p["color"] == whale_color for p in game_state["players"]): whale_color = random.choice(
                        WHALE_COLORS)
                    game_state["players"].append(
                        {"id": str(random.randint(10000, 99999)), "name": whale_name, "avatar": "", "bet": whale_bet,
                         "color": whale_color, "is_bot": True})
                    game_state["bank"] += whale_bet
                    game_state["whales_added"] += 1
                    game_state["last_bot_added"] = now
                    total_bank = game_state["bank"]
                    if total_bank > 0:
                        for p in game_state["players"]: p["percent"] = (p["bet"] / total_bank) * 100

        if time_left <= 0:
            game_state["status"] = "rolling"
            game_state["last_update"] = now
            total_bank = game_state["bank"]
            if total_bank > 0:
                for p in game_state["players"]: p["percent"] = (p["bet"] / total_bank) * 100
            else:
                for p in game_state["players"]: p["percent"] = 0

    elif game_state["status"] == "rolling":
        if now - game_state["last_update"] > 15.5:
            game_state["status"] = "finished"
            game_state["last_update"] = now
            if game_state["winner"]:
                history.insert(0, {"round_id": game_state["round_id"], "winner_name": game_state["winner"]["name"],
                                   "amount": game_state["bank"], "players": [
                        {"name": p["name"], "bet": p["bet"], "is_winner": p["id"] == game_state["winner"]["id"]} for p
                        in game_state["players"]]})
                if len(history) > 10: history.pop()

    elif game_state["status"] == "finished":
        if now - game_state["last_update"] > 5:
            if game_state["winner"] and not game_state["is_paid"]:
                game_state["is_paid"] = True
                winner_id = game_state["winner"]["id"]
                bank_amount = game_state["bank"]
                try:
                    res = supabase.table("users").select("coins").eq("user_id", int(winner_id)).execute()
                    if res.data:
                        supabase.table("users").update({"coins": int(res.data[0].get("coins", 0)) + bank_amount}).eq(
                            "user_id", int(winner_id)).execute()
                except Exception as e:
                    print(f"❌ Ошибка выплаты: {e}")
            game_state = {"status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
                          "last_update": now, "last_bot_added": 0, "target_bots": 0, "is_paid": False,
                          "round_id": str(int(time.time())), "whales_added": 0, "whale_chance": random.random() < 0.07}
            return game_state

    time_left = 15 - (now - game_state["last_update"]) if game_state["status"] == "waiting" else 0
    game_state["timer"] = max(0, int(time_left))
    return game_state


@app.post("/api/place_bet")
async def place_bet(data: PlaceBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
    if game_state["status"] != "waiting": raise HTTPException(status_code=400, detail="Ставки закрыты")
    if len(game_state["players"]) >= 20:
        existing = next((p for p in game_state["players"] if p["id"] == user_id), None)
        if not existing: raise HTTPException(status_code=400, detail="Комната заполнена")

    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    new_coins = current_coins - data.amount
    supabase.table("users").update({"coins": new_coins}).eq("user_id", int(user_id)).execute()

    name = user.get("username", user.get("first_name", f"id{user_id}"))
    avatar = user.get("photo_url", "")
    existing_player = next((p for p in game_state["players"] if p["id"] == user_id), None)

    if existing_player:
        existing_player["bet"] += data.amount
        game_state["bank"] += data.amount
    else:
        player_color = "#007aff"
        while any(p["color"] == player_color for p in
                  game_state["players"]): player_color = f"hsl({random.randint(0, 360)}, 80%, 50%)"
        game_state["players"].append(
            {"id": user_id, "name": name, "avatar": avatar, "bet": data.amount, "color": player_color, "is_bot": False})
        game_state["bank"] += data.amount

    total_bank = game_state["bank"]
    if total_bank > 0:
        for p in game_state["players"]: p["percent"] = (p["bet"] / total_bank) * 100
    return {"success": True, "new_balance": new_coins}


@app.post("/api/finish_hockey")
async def finish_hockey(data: FinishHockey):
    global game_state
    if game_state["status"] != "rolling": return {"success": False}
    total_bank = game_state["bank"]
    if total_bank == 0 or not game_state["players"]: return {"success": False}
    current_x = 0.0
    winner = None
    for p in game_state["players"]:
        exact_percent = (p["bet"] / total_bank) * 100.0
        if current_x <= data.x <= current_x + exact_percent + 0.0001:
            winner = p;
            break
        current_x += exact_percent
    if not winner and game_state["players"]: winner = game_state["players"][-1]
    game_state["winner"] = winner
    game_state["status"] = "finished"
    game_state["last_update"] = time.time()
    return {"success": True, "winner": winner, "bank": game_state["bank"]}


@app.get("/api/hockey_history")
async def get_hockey_history(): return history


# ==================== РАКЕТА (CRASH) ====================
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

            # НОВЫЙ ШАНС НА КРАШ:
            # 30% - краш на х1.0 - х2.0
            # 40% - краш на х2.0 - х5.0
            # 25% - краш на х5.0 - х10.0
            # 5% - краш на х10.0+
            rv = random.random()
            if rv < 0.30:
                cp = random.uniform(1.0, 2.0)
            elif rv < 0.70:
                cp = random.uniform(2.0, 5.0)
            elif rv < 0.95:
                cp = random.uniform(5.0, 10.0)
            else:
                cp = random.uniform(10.0, 100.0)

            total_user = sum(p["bet"] for p in crash_state["players"] if not p["is_bot"])
            if total_user > 5000: cp *= 0.7
            crash_state["crash_point"] = cp

    elif crash_state["status"] == "flying":
        # ПЛАВНЫЙ РОСТ
        speed = 0.05 * (crash_state["multiplier"] ** 1.3)
        crash_state["multiplier"] += speed

        for p in crash_state["players"]:
            if p["cashout_mult"] == 0 and p["is_bot"] and random.random() < 0.05:
                p["cashout_mult"] = crash_state["multiplier"]

        if crash_state["multiplier"] >= crash_state["crash_point"]:
            crash_state["status"] = "crashed"
            crash_state["status_start"] = now
            crash_state["last_update"] = now
            crash_history.insert(0, {"round_id": crash_state["round_id"],
                                     "crash_point": round(crash_state["crash_point"], 2), "players": [
                    {"name": p["name"], "bet": p["bet"], "cashout": p["cashout_mult"], "is_bot": p["is_bot"]} for p in
                    crash_state["players"]]})
            if len(crash_history) > 20: crash_history.pop()

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
    return {
        "status": crash_state["status"], "timer": crash_state["timer"],
        "multiplier": crash_state["multiplier"],
        "crash_point": round(crash_state["crash_point"], 2) if crash_state["status"] in ("flying", "crashed") else 0,
        "bank": crash_state["bank"], "round_id": crash_state["round_id"],
        "players": players_sorted, "history": crash_history[:10],
        "my_status": my_status, "my_bet": my_bet, "my_cashout_mult": my_cashout, "my_win": my_win,
    }


@app.post("/api/crash_bet")
async def crash_bet(data: CrashBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])
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
    # Строгая дата окончания: 1 ноября 2026 года, 15:00:00
    end_date = datetime.datetime(2026, 11, 1, 15, 0, 0)
    return {"end_timestamp": int(end_date.timestamp())}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))