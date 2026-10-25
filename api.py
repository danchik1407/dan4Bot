from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, hashlib, hmac, json, time, random, uvicorn, math
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ТВОИ ID
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
crash_state = {
    "status": "waiting",
    "timer": 10,
    "multiplier": 1.0,
    "last_update": time.time(),
    "crash_point": 0.0,
    "players": [],
    "bank": 0,
    "is_paid": False,
    "round_id": str(int(time.time())),
    "bots_added": 0,
    "target_bots": 0,
    "last_bot_added": 0
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
                while any(p["color"] == bot_color for p in game_state["players"]):
                    bot_color = random.choice(BOT_COLORS)

                game_state["players"].append({
                    "id": str(random.randint(10000, 99999)), "name": bot_name, "avatar": "",
                    "bet": bot_bet, "color": bot_color, "is_bot": True
                })
                game_state["bank"] += bot_bet
                game_state["last_bot_added"] = now
                game_state["target_bots"] = random.randint(1, 5)

                total_bank = game_state["bank"]
                if total_bank > 0:
                    for p in game_state["players"]:
                        p["percent"] = (p["bet"] / total_bank) * 100

            if game_state["whale_chance"] and game_state["whales_added"] < 2 and current_whales < 2 and len(
                    game_state["players"]) < 20 and (now - game_state["last_bot_added"]) > bot_delay:
                if random.random() < 0.3:
                    whale_bet = random.randint(500, 1000)
                    whale_name = random.choice(WHALE_NAMES) + str(random.randint(1, 9))
                    whale_color = random.choice(WHALE_COLORS)
                    while any(p["color"] == whale_color for p in game_state["players"]):
                        whale_color = random.choice(WHALE_COLORS)

                    game_state["players"].append({
                        "id": str(random.randint(10000, 99999)), "name": whale_name, "avatar": "",
                        "bet": whale_bet, "color": whale_color, "is_bot": True
                    })
                    game_state["bank"] += whale_bet
                    game_state["whales_added"] += 1
                    game_state["last_bot_added"] = now

                    total_bank = game_state["bank"]
                    if total_bank > 0:
                        for p in game_state["players"]:
                            p["percent"] = (p["bet"] / total_bank) * 100

        if time_left <= 0:
            game_state["status"] = "rolling"
            game_state["last_update"] = now
            total_bank = game_state["bank"]
            if total_bank > 0:
                for p in game_state["players"]:
                    p["percent"] = (p["bet"] / total_bank) * 100
            else:
                for p in game_state["players"]:
                    p["percent"] = 0

    elif game_state["status"] == "rolling":
        if now - game_state["last_update"] > 15.5:
            game_state["status"] = "finished"
            game_state["last_update"] = now

            if game_state["winner"]:
                history.insert(0, {
                    "round_id": game_state["round_id"],
                    "winner_name": game_state["winner"]["name"],
                    "amount": game_state["bank"],
                    "players": [{"name": p["name"], "bet": p["bet"], "is_winner": p["id"] == game_state["winner"]["id"]}
                                for p in game_state["players"]]
                })
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
                        current_coins = int(res.data[0].get("coins", 0))
                        new_coins = current_coins + bank_amount
                        supabase.table("users").update({"coins": new_coins}).eq("user_id", int(winner_id)).execute()
                except Exception as e:
                    print(f"❌ Ошибка выплаты: {e}")

            game_state = {
                "status": "waiting", "timer": 15, "bank": 0, "players": [],
                "winner": None, "last_update": now,
                "last_bot_added": 0, "target_bots": 0, "is_paid": False,
                "round_id": str(int(time.time())),
                "whales_added": 0,
                "whale_chance": random.random() < 0.07
            }
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
        if not existing:
            raise HTTPException(status_code=400, detail="Комната заполнена")

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
        while any(p["color"] == player_color for p in game_state["players"]):
            player_color = f"hsl({random.randint(0, 360)}, 80%, 50%)"

        game_state["players"].append({
            "id": user_id, "name": name, "avatar": avatar, "bet": data.amount,
            "color": player_color, "is_bot": False
        })
        game_state["bank"] += data.amount

    total_bank = game_state["bank"]
    if total_bank > 0:
        for p in game_state["players"]:
            p["percent"] = (p["bet"] / total_bank) * 100

    return {"success": True, "new_balance": new_coins}


@app.post("/api/finish_hockey")
async def finish_hockey(data: FinishHockey):
    global game_state
    if game_state["status"] != "rolling": return {"success": False}

    total_bank = game_state["bank"]
    if total_bank == 0 or not game_state["players"]:
        return {"success": False}

    current_x = 0.0
    winner = None
    for p in game_state["players"]:
        exact_percent = (p["bet"] / total_bank) * 100.0
        if current_x <= data.x <= current_x + exact_percent + 0.0001:
            winner = p
            break
        current_x += exact_percent

    if not winner and game_state["players"]:
        winner = game_state["players"][-1]

    game_state["winner"] = winner
    game_state["status"] = "finished"
    game_state["last_update"] = time.time()
    return {"success": True, "winner": winner, "bank": game_state["bank"]}


@app.get("/api/hockey_history")
async def get_hockey_history(): return history


# ==================== РАКЕТА (CRASH) ====================
@app.get("/api/crash_state")
async def get_crash_state():
    global crash_state, crash_history
    now = time.time()

    if crash_state["status"] == "waiting":
        time_left = 10 - (now - crash_state["last_update"])

        if time_left < -1: time_left = 0

        if time_left <= 8 and crash_state["target_bots"] == 0:
            crash_state["target_bots"] = random.randint(1, 3)

        if time_left <= 8 and crash_state["target_bots"] > 0:
            current_bots = sum(1 for p in crash_state["players"] if p["is_bot"])
            if current_bots < crash_state["target_bots"] and len(crash_state["players"]) < 20 and (
                    now - crash_state["last_bot_added"]) > 1.0:
                bot_bet = random.randint(10, 300)
                if random.random() < 0.8:
                    bot_bet = random.randint(10, 100)

                bot_name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
                crash_state["players"].append({
                    "id": f"bot_{random.randint(1000, 9999)}", "name": bot_name, "bet": bot_bet,
                    "cashout_mult": 0, "is_bot": True, "avatar": "", "auto_mult": 0
                })
                crash_state["bank"] += bot_bet
                crash_state["bots_added"] += 1
                crash_state["last_bot_added"] = now

        if time_left <= 0:
            crash_state["status"] = "flying"
            crash_state["last_update"] = now
            crash_state["multiplier"] = 1.0

            rand_val = random.random()
            if rand_val < 0.5:
                crash_state["crash_point"] = random.uniform(1.1, 2.0)
            elif rand_val < 0.8:
                crash_state["crash_point"] = random.uniform(2.0, 5.0)
            elif rand_val < 0.95:
                crash_state["crash_point"] = random.uniform(5.0, 10.0)
            else:
                crash_state["crash_point"] = random.uniform(10.0, 100.0)

            total_user_balance = sum(p["bet"] for p in crash_state["players"] if not p["is_bot"])
            if total_user_balance > 5000:
                crash_state["crash_point"] *= 0.7

    elif crash_state["status"] == "flying":
        elapsed = now - crash_state["last_update"]
        growth_speed = 0.1 + (crash_state["multiplier"] * 0.05)
        crash_state["multiplier"] += growth_speed * 0.1

        for p in crash_state["players"]:
            if p["cashout_mult"] == 0:
                if p["is_bot"] and random.random() < 0.05:
                    p["cashout_mult"] = crash_state["multiplier"]

        if crash_state["multiplier"] >= crash_state["crash_point"]:
            crash_state["status"] = "crashed"
            crash_state["last_update"] = now

            crash_history.insert(0, {
                "round_id": crash_state["round_id"],
                "crash_point": round(crash_state["crash_point"], 2),
                "players": [{"name": p["name"], "bet": p["bet"], "cashout": p["cashout_mult"]} for p in
                            crash_state["players"]]
            })
            if len(crash_history) > 20: crash_history.pop()

    elif crash_state["status"] == "crashed":
        if now - crash_state["last_update"] > 5:
            if not crash_state["is_paid"]:
                crash_state["is_paid"] = True
                for p in crash_state["players"]:
                    if p["cashout_mult"] > 0 and not p["is_bot"]:
                        win_amount = int(p["bet"] * p["cashout_mult"])
                        try:
                            res = supabase.table("users").select("coins").eq("user_id", int(p["id"])).execute()
                            if res.data:
                                current_coins = int(res.data[0].get("coins", 0))
                                supabase.table("users").update({"coins": current_coins + win_amount}).eq("user_id", int(
                                    p["id"])).execute()
                        except:
                            pass

            crash_state = {
                "status": "waiting", "timer": 10, "multiplier": 1.0, "last_update": now,
                "crash_point": 0.0, "players": [], "bank": 0, "is_paid": False,
                "round_id": str(int(time.time())),
                "bots_added": 0, "target_bots": 0, "last_bot_added": 0
            }
            return crash_state

    time_left = 10 - (now - crash_state["last_update"]) if crash_state["status"] == "waiting" else 0
    crash_state["timer"] = max(0, int(time_left))
    return crash_state


@app.post("/api/crash_bet")
async def crash_bet(data: CrashBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])

    if crash_state["status"] != "waiting": raise HTTPException(status_code=400, detail="Ставки закрыты")

    existing = next((p for p in crash_state["players"] if p["id"] == user_id), None)
    if existing: raise HTTPException(status_code=400, detail="Вы уже сделали ставку")

    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    supabase.table("users").update({"coins": current_coins - data.amount}).eq("user_id", int(user_id)).execute()

    name = user.get("username", f"id{user_id}")
    avatar = user.get("photo_url", "")

    crash_state["players"].append({
        "id": user_id, "name": name, "bet": data.amount, "cashout_mult": 0, "is_bot": False, "avatar": avatar,
        "auto_mult": 0
    })
    crash_state["bank"] += data.amount

    return {"success": True, "new_balance": current_coins - data.amount}


@app.post("/api/crash_cashout")
async def crash_cashout(data: CrashCashout):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = str(user["id"])

    if crash_state["status"] != "flying": raise HTTPException(status_code=400, detail="Игра не идет")

    player = next((p for p in crash_state["players"] if p["id"] == user_id), None)
    if not player or player["cashout_mult"] > 0: raise HTTPException(status_code=400, detail="Вы уже забрали")

    player["cashout_mult"] = crash_state["multiplier"]
    win_amount = int(player["bet"] * player["cashout_mult"])

    return {"success": True, "win_amount": win_amount, "mult": player["cashout_mult"], "new_balance": int(
        supabase.table("users").select("coins").eq("user_id", int(user_id)).execute().data[0].get("coins",
                                                                                                  0)) + win_amount}


@app.get("/api/crash_history")
async def get_crash_history(): return crash_history


# ==================== ЮЗЕР И ЭКОНОМИКА ====================
@app.post("/api/user")
async def get_user(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]

    # Скрываем баланс, если запрашивают админа (но не самого себя)
    if user_id in ADMIN_IDS and user_id != user.get("id"):
        return {"coins": -1, "keys": 0, "inventory": [], "last_chest": 0, "hidden": True}

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
    user_id = user["id"];
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
    current_coins = int(res.data[0].get("coins", 0));
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
    current_coins = int(res.data[0].get("coins", 0));
    new_coins = max(0, current_coins + data.amount)
    supabase.table("users").update({"coins": new_coins}).eq("user_id", user_id).execute()
    return {"success": True, "new_balance": new_coins}


@app.post("/api/update_keys")
async def update_keys(data: UpdateKeys):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    res = supabase.table("users").select("keys").eq("user_id", user_id).execute()
    current_keys = int(res.data[0].get("keys", 0) or 0);
    new_keys = max(0, current_keys + data.amount)
    supabase.table("users").update({"keys": new_keys}).eq("user_id", user_id).execute()
    return {"success": True, "new_keys": new_keys}


@app.get("/api/global_top")
async def get_global_top():
    res = supabase.table("users").select("user_id, username, coins").order("coins", desc=True).limit(50).execute()
    filtered = [u for u in res.data if int(u["user_id"]) not in ADMIN_IDS]
    return filtered[:50]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))