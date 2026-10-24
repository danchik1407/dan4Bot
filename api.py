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

BOT_NAMES = ["Shark_V", "LuckyFox", "NeonWolf", "GhostByte", "PixelKing", "CyberCat", "IronFist", "VoidWalker",
             "ShadowAce", "RogueOne"]
BOT_COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#5856d6", "#af52de", "#ff2d55"]

game_state = {
    "status": "waiting",
    "timer": 20,
    "bank": 0,
    "players": [],
    "ball": {"x": 50, "y": 50, "vx": 0, "vy": 0},
    "winner": None,
    "last_update": time.time(),
    "last_bot_added": 0
}


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


class PlaceBet(BaseModel): initData: str; amount: int


@app.get("/api/hockey_state")
async def get_hockey_state():
    global game_state
    now = time.time()
    time_left = 20 - (now - game_state["last_update"])

    if game_state["status"] == "waiting":
        # ЛОГИКА ПЛАВНОГО ДОБАВЛЕНИЯ БОТОВ
        # Если до конца осталось меньше 6 секунд, и ботов меньше 5
        if time_left <= 6 and len(game_state["players"]) < 20:
            current_bots = sum(1 for p in game_state["players"] if p["is_bot"])

            # Если прошло больше 1 секунды с момента последнего добавления бота
            if current_bots < 5 and (now - game_state["last_bot_added"]) > 1.0:
                # Добавляем ОДНОГО бота
                bot_bet = random.randint(10, 100)
                bot_name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
                bot_color = random.choice(BOT_COLORS)

                while any(p["color"] == bot_color for p in game_state["players"]):
                    bot_color = random.choice(BOT_COLORS)

                bot_shape = random.choice(["circle", "square", "oval", "triangle"])
                game_state["players"].append({
                    "id": random.randint(10000, 99999), "name": bot_name, "avatar": "", "bet": bot_bet,
                    "color": bot_color, "shape": bot_shape, "is_bot": True
                })
                game_state["bank"] += bot_bet
                game_state["last_bot_added"] = now

        if time_left <= 0:
            game_state["status"] = "rolling"
            game_state["last_update"] = now

            total_bank = game_state["bank"]
            if total_bank > 0:
                for p in game_state["players"]:
                    p["percent"] = round((p["bet"] / total_bank) * 100, 1)
            else:
                for p in game_state["players"]:
                    p["percent"] = 0

            rand_val = random.uniform(0, 100)
            current_val = 0
            for p in game_state["players"]:
                current_val += p["percent"]
                if rand_val <= current_val:
                    game_state["winner"] = p
                    break

            angle = random.uniform(0, 2 * math.pi)
            game_state["ball"]["vx"] = math.cos(angle) * 10
            game_state["ball"]["vy"] = math.sin(angle) * 10

    elif game_state["status"] == "rolling":
        if now - game_state["last_update"] > 7:
            game_state["status"] = "finished"
            game_state["last_update"] = now

    elif game_state["status"] == "finished":
        if now - game_state["last_update"] > 4:
            game_state = {
                "status": "waiting", "timer": 20, "bank": 0, "players": [],
                "ball": {"x": 50, "y": 50, "vx": 0, "vy": 0},
                "winner": None, "last_update": now,
                "last_bot_added": 0
            }

    game_state["timer"] = max(0, int(time_left) if game_state["status"] == "waiting" else 0)
    return game_state


@app.post("/api/place_bet")
async def place_bet(data: PlaceBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    if game_state["status"] != "waiting": raise HTTPException(status_code=400, detail="Ставки закрыты")

    res = supabase.table("users").select("coins").eq("user_id", user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    current_coins = int(res.data[0].get("coins", 0))
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    new_coins = current_coins - data.amount
    supabase.table("users").update({"coins": new_coins}).eq("user_id", user_id).execute()

    name = user.get("username", user.get("first_name", f"id{user_id}"))
    avatar = user.get("photo_url", "")

    existing_player = next((p for p in game_state["players"] if p["id"] == user_id and not p["is_bot"]), None)

    if existing_player:
        existing_player["bet"] += data.amount
        game_state["bank"] += data.amount
    else:
        if len(game_state["players"]) >= 20: raise HTTPException(status_code=400, detail="Комната заполнена")

        player_color = "#007aff"
        while any(p["color"] == player_color for p in game_state["players"]):
            player_color = f"hsl({random.randint(0, 360)}, 80%, 50%)"

        game_state["players"].append({
            "id": user_id, "name": name, "avatar": avatar, "bet": data.amount,
            "color": player_color, "shape": "square", "is_bot": False
        })
        game_state["bank"] += data.amount

    return {"success": True, "new_balance": new_coins}


@app.post("/api/user")
async def get_user(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if not res.data:
        new_user = {"user_id": user_id, "coins": 0, "keys": 0, "username": user.get("username", f"id{user_id}"),
                    "last_chest": 0, "chat_id": 0, "inventory": "[]"}
        supabase.table("users").insert(new_user).execute()
        return {"coins": 0, "keys": 0, "inventory": [], "last_chest": 0}
    db_user = res.data[0]
    return {"coins": int(db_user.get("coins", 0)), "keys": int(db_user.get("keys", 0)),
            "inventory": json.loads(db_user.get("inventory") or "[]"), "last_chest": int(db_user.get("last_chest", 0))}


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


@app.get("/api/global_top")
async def get_global_top():
    res = supabase.table("users").select("username, coins").order("coins", desc=True).limit(10).execute()
    return res.data


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))