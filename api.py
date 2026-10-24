from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os, hashlib, hmac, json, time, random, uvicorn
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"],
                   allow_headers=["*"])

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
BOT_TOKEN = os.getenv("BOT_TOKEN")

crash_state = {
    "status": "waiting",
    "start_time": 0,
    "current_coef": 1.0,
    "crash_point": 0.0,
    "last_round": 0
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


class UpdateKeys(BaseModel): initData: str; amount: int


class BuyItem(BaseModel): initData: str; item_id: str; price: int


@app.get("/api/crash_state")
async def get_crash_state():
    global crash_state
    now = time.time()

    if crash_state["status"] == "waiting":
        if now - crash_state["last_round"] > 10:  # 10 секунд ожидания
            crash_state["status"] = "flying"
            crash_state["start_time"] = now
            crash_state["current_coef"] = 1.0
            crash_state["crash_point"] = round(1.0 + (random.random() ** 1.5) * 25, 2)

    elif crash_state["status"] == "flying":
        elapsed = now - crash_state["start_time"]
        # Экспоненциальный рост для ощущения скорости
        current = 1.0 + (elapsed ** 1.8) * 0.2
        if current >= crash_state["crash_point"]:
            crash_state["status"] = "crashed"
            crash_state["current_coef"] = crash_state["crash_point"]
            crash_state["last_round"] = now
        else:
            crash_state["current_coef"] = round(current, 2)

    elif crash_state["status"] == "crashed":
        if now - crash_state["last_round"] > 4:  # 4 секунды после краша
            crash_state["status"] = "waiting"
            crash_state["last_round"] = now

    return crash_state


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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))