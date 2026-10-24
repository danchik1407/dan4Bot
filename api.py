from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import hashlib
import hmac
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client
import json

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def verify_telegram_data(init_data: str):
    try:
        parsed_data = dict(parse_qsl(init_data))
        if "hash" not in parsed_data:
            return None

        hash_ = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash == hash_:
            user_data = json.loads(parsed_data.get("user", "{}"))
            return user_data
        return None
    except Exception as e:
        print(f"Auth Error: {e}")
        return None


class UserAuth(BaseModel):
    initData: str


class UpdateBalance(BaseModel):
    initData: str
    amount: int


@app.post("/api/user")
async def get_user(auth: UserAuth):
    user = verify_telegram_data(auth.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")

    user_id = user["id"]
    username = user.get("username", f"id{user_id}")

    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    if not res.data:
        new_user = {
            "user_id": user_id, "coins": 0, "keys": 0, "username": username,
            "last_chest": 0, "last_call": 0, "call_sign": None, "chat_id": 0
        }
        supabase.table("users").insert(new_user).execute()
        return {"coins": 0, "keys": 0}

    db_user = res.data[0]
    return {"coins": db_user.get("coins", 0), "keys": db_user.get("keys", 0)}


@app.post("/api/update_balance")
async def update_balance(data: UpdateBalance):
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")

    user_id = user["id"]

    res = supabase.table("users").select("coins").eq("user_id", user_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")

    current_coins = res.data[0]["coins"]
    new_coins = current_coins + data.amount

    if new_coins < 0:
        new_coins = 0

    supabase.table("users").update({"coins": new_coins}).eq("user_id", user_id).execute()
    return {"success": True, "new_balance": new_coins}


# Эндпоинт для обновления ключей (для сундуков)
class UpdateKeys(BaseModel):
    initData: str
    amount: int


@app.post("/api/update_keys")
async def update_keys(data: UpdateKeys):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")

    user_id = user["id"]
    res = supabase.table("users").select("keys").eq("user_id", user_id).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")

    current_keys = res.data[0].get("keys", 0)
    new_keys = current_keys + data.amount

    if new_keys < 0: new_keys = 0

    supabase.table("users").update({"keys": new_keys}).eq("user_id", user_id).execute()
    return {"success": True, "new_keys": new_keys}