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

game_state = {
    "status": "waiting", "timer": 5, "bank": 0, "players": [],
    "ball": {"x": 50, "y": 50, "vx": 0, "vy": 0}, "winner": None, "last_update": time.time()
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

    if game_state["status"] == "waiting":
        if now - game_state["last_update"] > 5:
            game_state["status"] = "rolling"
            game_state["last_update"] = now
            angle = random.uniform(0, 2 * math.pi)
            game_state["ball"]["vx"] = math.cos(angle) * 3
            game_state["ball"]["vy"] = math.sin(angle) * 3

    elif game_state["status"] == "rolling":
        ball = game_state["ball"]
        ball["x"] += ball["vx"]
        ball["y"] += ball["vy"]

        if ball["x"] < 0 or ball["x"] > 100: ball["vx"] *= -1
        if ball["y"] < 0 or ball["y"] > 100: ball["vy"] *= -1

        for p in game_state["players"]:
            if abs(ball["x"] - p["x"]) < 10 and abs(ball["y"] - p["y"]) < 10:
                game_state["status"] = "finished"
                game_state["winner"] = p
                game_state["last_update"] = now
                break

        if now - game_state["last_update"] > 5:
            game_state["status"] = "finished"
            game_state["winner"] = random.choice(game_state["players"]) if game_state["players"] else None
            game_state["last_update"] = now

    elif game_state["status"] == "finished":
        if now - game_state["last_update"] > 4:
            game_state = {
                "status": "waiting", "timer": 5, "bank": 0, "players": [],
                "ball": {"x": 50, "y": 50, "vx": 0, "vy": 0}, "winner": None, "last_update": now
            }

    game_state["timer"] = max(0, 5 - int(now - game_state["last_update"]))
    return game_state


@app.post("/api/place_bet")
async def place_bet(data: PlaceBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    if game_state["status"] != "waiting": raise HTTPException(status_code=400, detail="Ставки закрыты")

    res = supabase.table("users").select("coins").eq("user_id", user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    if res.data[0]["coins"] < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    new_coins = res.data[0]["coins"] - data.amount
    supabase.table("users").update({"coins": new_coins}).eq("user_id", user_id).execute()

    name = user.get("username", user.get("first_name", f"id{user_id}"))
    avatar = user.get("photo_url", "")
    x, y = random.choice([15, 30, 70, 85]), random.choice([15, 30, 70, 85])

    game_state["players"].append({
        "id": user_id, "name": name, "avatar": avatar, "bet": data.amount,
        "x": x, "y": y, "color": f"hsl({random.randint(0, 360)}, 80%, 50%)"
    })
    game_state["bank"] += data.amount

    while len(game_state["players"]) < 4:
        bot_bet = random.randint(10, 50)
        game_state["players"].append({
            "id": random.randint(10000, 99999), "name": f"Bot_{random.randint(1, 99)}", "avatar": "", "bet": bot_bet,
            "x": random.choice([15, 30, 70, 85]), "y": random.choice([15, 30, 70, 85]),
            "color": f"hsl({random.randint(0, 360)}, 80%, 50%)"
        })
        game_state["bank"] += bot_bet

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
    return {"coins": db_user.get("coins", 0), "keys": db_user.get("keys", 0),
            "inventory": json.loads(db_user.get("inventory") or "[]"), "last_chest": db_user.get("last_chest", 0)}


@app.post("/api/open_chest")
async def open_chest(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    now = int(time.time())
    res = supabase.table("users").select("last_chest").eq("user_id", user_id).execute()
    if (now - (res.data[0].get("last_chest", 0) or 0)) < 18000: raise HTTPException(status_code=400, detail="Cooldown")
    supabase.table("users").update({"last_chest": now}).eq("user_id", user_id).execute()
    return {"success": True}


@app.post("/api/buy_item")
async def buy_item(data: BuyItem):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    res = supabase.table("users").select("coins, inventory").eq("user_id", user_id).execute()
    current_coins = res.data[0]["coins"]
    inventory = json.loads(res.data[0].get("inventory") or "[]")
    if current_coins < data.price: raise HTTPException(status_code=400, detail="Not enough coins")
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
    new_coins = max(0, res.data[0]["coins"] + data.amount)
    supabase.table("users").update({"coins": new_coins}).eq("user_id", user_id).execute()
    return {"success": True, "new_balance": new_coins}


@app.post("/api/update_keys")
async def update_keys(data: UpdateKeys):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = user["id"]
    res = supabase.table("users").select("keys").eq("user_id", user_id).execute()
    new_keys = max(0, (res.data[0].get("keys", 0) or 0) + data.amount)
    supabase.table("users").update({"keys": new_keys}).eq("user_id", user_id).execute()
    return {"success": True, "new_keys": new_keys}


@app.get("/api/global_top")
async def get_global_top():
    res = supabase.table("users").select("username, coins").order("coins", desc=True).limit(10).execute()
    return res.data


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))