from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import uuid
import logging
import os, hashlib, hmac, json, time, random, secrets, uvicorn, datetime, math, urllib.request, urllib.parse
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client
from typing import Optional, Union
from shop_catalog import SHOP_ITEMS, ShopPurchaseError, is_admin_id, parse_inventory, purchase_shop_item, \
    serialize_shop_items, missing_stock_rows, shop_stock_snapshot, stock_diagnostics, \
    sold_out_items, STOCK_LIMITS_ENABLED
from love import (LOVE_STAGES, LOVE_ACTION_BONUS, LOVE_ACTION_COOLDOWN, LOVE_ICE_COST,
                  RP_ACTIONS, claim_stage_rewards, stage_info as love_stage_info,
                  next_stage as love_next_stage)

load_dotenv()

# ==================== HTTP-КЛИЕНТ ДЛЯ TELEGRAM ====================
# aiohttp НЕ обязателен. Сервис aura-api на Render собирается командой
# `pip install fastapi uvicorn supabase python-dotenv` (без requirements.txt),
# и там aiohttp нет — из-за `import aiohttp` на верхнем уровне uvicorn падал
# на старте с ModuleNotFoundError. Клиент выбирается в рантайме:
# httpx (ставится вместе с supabase) → aiohttp (если всё-таки установлен) →
# стандартный urllib в отдельном потоке.
try:
    import httpx as _httpx
except Exception:  # pragma: no cover - зависит от сборки Render
    _httpx = None
try:
    import aiohttp as _aiohttp
except Exception:  # pragma: no cover - зависит от сборки Render
    _aiohttp = None
HTTP_CLIENT = "httpx" if _httpx is not None else ("aiohttp" if _aiohttp is not None else "urllib")


async def _http_json(url, method: str = "GET", params=None, payload=None, timeout: float = 8.0):
    """Запросить JSON у Telegram Bot API любым доступным клиентом.

    Ошибки сети пробрасываются наружу: вызывающий код сам решает, отдать ли
    игроку «попробуйте позже» (см. telegram_chat_member_status / stars_invoice).
    """
    limit = float(timeout or 8.0)
    method = (method or "GET").upper()

    if _httpx is not None:
        async with _httpx.AsyncClient(timeout=limit) as client:
            if method == "POST":
                response = await client.post(url, json=payload)
            else:
                response = await client.get(url, params=params)
            return response.json()

    if _aiohttp is not None:
        client_timeout = _aiohttp.ClientTimeout(total=limit)
        async with _aiohttp.ClientSession(timeout=client_timeout) as session:
            if method == "POST":
                async with session.post(url, json=payload) as response:
                    return await response.json(content_type=None)
            async with session.get(url, params=params) as response:
                return await response.json(content_type=None)

    def _blocking():
        if method == "POST":
            body = json.dumps(payload or {}).encode("utf-8")
            request = urllib.request.Request(url, data=body,
                                             headers={"Content-Type": "application/json"})
        else:
            full = url
            if params:
                full = f"{url}?{urllib.parse.urlencode(params)}"
            request = urllib.request.Request(full)
        with urllib.request.urlopen(request, timeout=limit) as response:
            return json.loads(response.read().decode("utf-8"))

    return await asyncio.to_thread(_blocking)


# Номер сборки: по нему из /api/health видно, что на сервисе НЕ старый код.
# Если после деплоя здесь прежнее значение — Render ещё не перезапустился.
API_BUILD = "2026-11-19-arena-slider-15"

# Момент старта процесса. По нему видно, что сервис только что «проснулся»:
# Render на бесплатном тарифе усыпляет Web Service через 15 минут без входящих
# запросов, а первый запрос после сна показывает экран «SERVICE WAKING UP».
APP_STARTED_AT = time.time()

logger = logging.getLogger("aura-api")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# Схему и документацию уводим под /api: корневые /docs и /openapi.json заняты
# статикой Mini App, а «Not Found» на них только путал при настройке.
app = FastAPI(title="AURA API", docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")

# CORS: мини-аппка живёт на этом же сервисе, но запросы к API приходят из WebView
# Telegram (Origin может быть https://web.telegram.org, t.me или вообще отсутствовать),
# поэтому разрешаем все источники. Куки/сессии не используем — авторизация только
# через подписанный initData, так что "*" здесь безопасен.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

# ==================== СТАТИКА MINI APP ====================
# Архитектура проекта — ДВА сервиса Render:
#   aura-api (этот файл) — FastAPI + раздача miniapp (index.html/app.js/style.css);
#   dan4Bot             — aiogram (bot.py), статику не раздаёт.
# Путь считаем от самого файла: на Render рабочий каталог может отличаться,
# из-за этого раньше статика могла «не подняться» и Mini App отдавал 404.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MINIAPP_DIR = os.path.join(BASE_DIR, "miniapp")

if os.path.isdir(MINIAPP_DIR):
    # То же самое, что app.mount("/miniapp", StaticFiles(directory="miniapp", html=True)),
    # только путь абсолютный: на Render рабочий каталог процесса может отличаться от корня репозитория.
    app.mount("/miniapp", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
else:  # pragma: no cover — защита от кривого деплоя
    logger.error("Папка miniapp не найдена (%s): Mini App не будет раздаваться", MINIAPP_DIR)


@app.get("/", include_in_schema=False)
async def root_redirect(request: Request):
    """Корень сервиса: человеку — Mini App, мониторингу — «alive».

    Именно 307-редирект, а не отдача index.html с «/»: в index.html пути к
    style.css/app.js относительные, и с корня они превратились бы в /style.css (404).

    Но UptimeRobot и подобные мониторы по умолчанию бьют в корень и просят
    `Accept: */*`. Им отдаём тот же дешёвый JSON, что /api/ping, иначе после
    редиректа на HTML монитор с keyword `alive` показывал бы «Down».
    """
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return await ping()
    return RedirectResponse(url="/miniapp/", status_code=307)


# Файлы, которые исторически пытаются открыть по корню сервиса.
# Например, в BotFather указали https://aura-api-....onrender.com/index.html —
# раньше это был голый «Not Found», теперь редирект в реальную папку статики.
ROOT_STATIC_FILES = {
    "index.html", "app.js", "style.css", "coin.png",
    "chest_normal.png", "chest_gold.png", "avatar_placeholder.svg",
}


# Telegram WebView кэширует статику агрессивно: игрок мог видеть СТАРЫЙ app.js
# (в нём оставались старые тексты и старая логика круга), хотя сервер уже отдавал
# новый. Теперь index.html всегда перепроверяется, а JS/CSS кэшируются по версии
# в адресе (?v=aura-9): сменили версию — браузер скачает файл заново.
@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path in ("/miniapp", "/miniapp/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Пустой ответ вместо 404 в логах и в консоли браузера."""
    return Response(status_code=204)


@app.get("/api/ping")
async def ping():
    """Самый дешёвый ответ: ни базы, ни тиража, ни кэшей.

    Нужен для прогрева: Render на бесплатном тарифе усыпляет сервис через
    15 минут без входящих запросов, и тогда игрок вместо игры видит экран
    «SERVICE WAKING UP / START BUILDING ON RENDER TODAY». Пинг раз в несколько
    минут не даёт уснуть. Именно ЭТОТ путь, а не /api/health: health читает
    тираж капсул из Supabase, а прогрев не должен тратить на это лимиты.
    """
    return {
        "ok": True,
        # Явный статус для мониторинга (UptimeRobot и т.п.): {"ok": true, "status": "alive"}.
        "status": "alive",
        "service": "aura-api",
        "build": API_BUILD,
        # Сколько секунд процесс живёт: маленькое число = сервис только что проснулся.
        "uptime_seconds": int(max(0, time.time() - APP_STARTED_AT)),
    }


@app.get("/health", include_in_schema=False)
async def health_alias():
    """Тот же дешёвый ответ, что /api/ping.

    Многие мониторинги (UptimeRobot в том числе) по умолчанию проверяют `/health`.
    Ни базы, ни статики — только «жив ли процесс».
    """
    return await ping()


# Кэш состояния тиража для /api/health: {at, value, error}.
_stock_health_cache = {"at": 0.0, "value": None, "error": None}
STOCK_HEALTH_TTL = 20.0


def _stock_health_hint(stock: dict | None, stock_error: str | None) -> str | None:
    """Одна короткая подсказка: что именно применить в Supabase."""
    if stock is None:
        return ("таблица тиража недоступна: " + stock_error if stock_error else
                "примените supabase/migrations/20261104000000_shop_stock_fix.sql")
    missing = missing_stock_rows(stock)
    if missing:
        return ("нет строк тиража: " + ", ".join(missing) +
                " — примените supabase/migrations/20261104000000_shop_stock_fix.sql")
    return None


@app.get("/api/health")
async def health():
    """Проверка сервиса: Render health check + админская диагностика в боте."""
    # Состояние тиража: если магазин продаёт, а «Осталось» не меняется — видно здесь.
    # Кэш на 20 секунд: Render дергает /api/health часто, а лишние запросы к базе
    # на каждый пинг не нужны.
    global _stock_health_cache
    now_ts = time.time()
    cache_ttl = STOCK_HEALTH_TTL if _stock_health_cache["value"] is not None else 5.0
    if not STOCK_LIMITS_ENABLED:
        # Лимитов нет — тираж не читаем вообще (нечего проверять).
        _stock_health_cache = {"at": now_ts, "value": None, "error": None}
    elif now_ts - _stock_health_cache["at"] > cache_ttl:
        try:
            _stock_health_cache = {"at": now_ts, "value": await asyncio.to_thread(shop_stock_snapshot, supabase),
                                   "error": None}
        except Exception as exc:  # noqa: BLE001
            _stock_health_cache = {"at": now_ts, "value": None,
                                   "error": f"{type(exc).__name__}: {exc}"[:300]}
    stock = _stock_health_cache["value"]
    stock_error = _stock_health_cache["error"]
    return {
        "ok": True,
        # Мониторинг: UptimeRobot ждёт "status": "alive" (см. §1 DEPLOY.md).
        "status": "alive",
        "service": "aura-api",
        "build": API_BUILD,
        # Какой HTTP-клиент достался сборке: httpx/aiohttp/urllib. Если тут
        # urllib — на сервисе нет ни httpx, ни aiohttp (проверьте build command).
        "http_client": HTTP_CLIENT,
        # Прогрев: по uptime видно, спал ли сервис (Render усыпляет free-инстанс
        # через 15 минут без запросов и показывает свой экран «SERVICE WAKING UP»).
        "awake": {
            "uptime_seconds": int(max(0, now_ts - APP_STARTED_AT)),
            "just_woke": (now_ts - APP_STARTED_AT) < 180,
            "warmup_endpoint": "/api/ping",
        },
        "static": {
            "dir": "miniapp",
            "ready": os.path.isfile(os.path.join(MINIAPP_DIR, "index.html")),
            "url": "/miniapp/",
        },
        "shop_stock": {
            # Задача 19: лимиты включены — у каждой капсулы общий тираж.
            # false бывает только если в shop_catalog.py флаг выключили вручную.
            "limits_enabled": STOCK_LIMITS_ENABLED,
            "ready": stock is not None,
            "left": stock,
            "rpc": "shop_take_stock",
            "debug": "/api/stock_debug",
            "error": stock_error,
            # Строки тиража, которых нет в таблице. Непустой список — это ровно тот
            # случай, когда магазин рисовал «Осталось: 1000 из 1000», а покупка
            # отвечала «Товар распродан»: списывать было нечего.
            "missing_rows": missing_stock_rows(stock) if STOCK_LIMITS_ENABLED else [],
            "hint": _stock_health_hint(stock, stock_error) if STOCK_LIMITS_ENABLED else
            "лимиты выключены в коде: капсулы продаются без ограничений",
        },
        # Шансы сундуков: по ним видно, что на сервисе нужная сборка.
        "chests": {
            "normal": {
                "coins": [CHEST_MIN_COINS, CHEST_MAX_COINS],
                "key_chance": CHEST_KEY_CHANCE,
                "capsule_chance": CHEST_CAPSULE_CHANCE_NORMAL,
                "capsule": CHEST_CAPSULE_ITEM,
            },
            "gold": {
                "coins": [GOLD_MIN_COINS, GOLD_MAX_COINS],
                "key_back_chance": GOLD_KEY_BACK_CHANCE,
                "capsule_chance": CHEST_CAPSULE_CHANCE_GOLD,
                "capsule": CHEST_CAPSULE_ITEM,
            },
        },
        # MAINTENANCE_MODE объявлен ниже по файлу, но к моменту запроса уже существует.
        "maintenance": MAINTENANCE_MODE,
        # Доступ: пока ADMIN_ONLY_APP включён, Mini App открывают только админы.
        "access": {
            "admin_only_app": ADMIN_ONLY_APP,
            "admins": len(ADMIN_IDS),
            "message": ADMIN_ONLY_MESSAGE if ADMIN_ONLY_APP else "",
        },
        # Экономика задачи 25: по этим числам видно нужную сборку на сервисе.
        "economy": {
            "subscription_bonus": SUBSCRIPTION_BONUS,
            "channel": f"@{CHANNEL_USERNAME}",
            "passive_rates": PASSIVE_INCOME_RATES,
            "passive_max_hours": PASSIVE_INCOME_MAX_HOURS,
            "referral_rewards": {
                "base": REFERRAL_REWARD_BASE,
                "nft2": REFERRAL_REWARD_NFT2,
                "premium": REFERRAL_REWARD_PREMIUM,
            },
            "stars_enabled": STARS_ENABLED,
            "upgrade": _upgrade_economy_config(),
            "arena_mode": game_state.get("mode", "normal"),
        },
    }


# service-role ключ (если задан) обходит RLS: без него запись в promocodes,
# chest_claims и capsule_stock может упасть с «violates row-level security policy».
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(os.getenv("SUPABASE_URL"), SUPABASE_SERVICE_KEY or os.getenv("SUPABASE_KEY"))
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = [6338018270, 5184813347, 8890834384]
MAX_BET = 10000

# ==================== ДОСТУП В MINI APP ====================
# Задача 24+: пока идут работы, в Mini App заходят ТОЛЬКО администраторы
# (ADMIN_IDS). Это отдельный, «жёсткий» рубильник, который НЕ выключается
# флагом техработ: выключается переменной окружения ADMIN_ONLY_APP=0 в Render.
#   ADMIN_ONLY_APP=1 (по умолчанию) — заходят только админы, остальные видят
#   экран «Технические работы», а API отвечает им 403;
#   ADMIN_ONLY_APP=0 — заходят все (старое поведение).
def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


ADMIN_ONLY_APP = _env_flag("ADMIN_ONLY_APP", True)
ADMIN_ONLY_MESSAGE = ("🛠️ Идут технические работы. Пока доступ к игре есть "
                      "только у администраторов — скоро откроем всем.")

# ==================== ПОДПИСКА НА КАНАЛ ====================
# Бонус начисляется РОВНО ОДИН РАЗ за всё время: факт начисления хранится в
# таблице subscriptions_history (user_id — первичный ключ). Отписался и
# подписался снова — бонуса не будет.
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "Auragameplay_Channel").strip().lstrip("@")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"
SUBSCRIPTION_BONUS = 200
SUBSCRIPTIONS_TABLE = "subscriptions_history"

# ==================== ПАССИВНЫЙ ДОХОД С КАПСУЛ ====================
# Сколько коинов в час приносит одна капсула каждого типа.
PASSIVE_INCOME_RATES = {
    "capsule": 0.1,
    "uniq_capsule": 0.5,
    "ruby": 5.0,
    "diamond": 20.0,
    "galactic": 50.0,
}
# Максимум накопления: если игрок не заходил неделю, «проценты» за 168 часов
# не капают — иначе один заход давал бы миллионы.
PASSIVE_INCOME_MAX_HOURS = 24.0
PASSIVE_INCOME_MIN_HOURS = 1.0 / 60.0     # меньше минуты не считаем (шум)
PASSIVE_INCOME_TABLE = "passive_income"

# ==================== РЕФЕРАЛЬНАЯ СИСТЕМА ====================
REFERRALS_TABLE = "referrals"
REFERRAL_REWARD_BASE = 500          # обычный реферал
REFERRAL_REWARD_NFT2 = 800          # реферал с 2+ NFT
REFERRAL_REWARD_PREMIUM = 2000      # premium с рейтингом и 1+ NFT
# «NFT» в этом проекте — лимитированные капсулы (их тираж конечен, как у NFT),
# а всё, что начинается с nft, тоже считается. Premium — колонка users.premium
# (если её нет, реферал считается обычным).
REFERRAL_NFT_ITEMS = {"galactic", "diamond"}

# ==================== TELEGRAM STARS (донат) ====================
# Заглушка прайса: цены согласуем позже. Пока пакеты описаны здесь, а покупка
# включается переменной окружения STARS_ENABLED=1.
STARS_ENABLED = _env_flag("STARS_ENABLED", False)
STARS_PACKAGES = [
    {"id": "coins_1000", "coins": 1000, "stars": 5},
    {"id": "coins_5000", "coins": 5000, "stars": 20},
    {"id": "coins_15000", "coins": 15000, "stars": 50},
]
STARS_PAYMENTS_TABLE = "stars_payments"

# ==================== IDEMPOTENCY ====================
# Ключ запроса от клиента (request_id) + таблица idempotency_keys: повторная
# отправка той же операции (двойной тап, ретрай после таймаута) не спишет
# деньги дважды, а вернёт сохранённый ответ.
IDEMPOTENCY_TABLE = "idempotency_keys"
# Сколько живёт сохранённый ответ. Через сутки ключ можно переиспользовать.
IDEMPOTENCY_TTL = 86400

# ==================== ТЕХРАБОТЫ ====================
# Техработы ЗАКОНЧИЛИСЬ: играть может любой игрок (раньше — только ADMIN_IDS).
# Флаг оставлен на случай срочного закрытия: поставь в Render MAINTENANCE_MODE=1,
# и приложение снова будет пускать только админов (экран «Технические работы» +
# 403 на любые игровые действия — обойти запросом к API нельзя).
# Апгрейд от этого флага НЕ зависит: он отдельно закрыт списком ADMIN_IDS
# (см. UPGRADE_ADMIN_ONLY ниже).
def _maintenance_enabled() -> bool:
    raw = (os.getenv("MAINTENANCE_MODE") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    return False  # "0"/"false"/пусто — техработы выключены, играют все


MAINTENANCE_MODE = _maintenance_enabled()
MAINTENANCE_MESSAGE = "🛠️ Технические работы: скоро вернёмся"


def can_play(user_id) -> bool:
    """Может ли этот пользователь играть прямо сейчас.

    Два независимых рубильника:
      1) ADMIN_ONLY_APP — «в аппку заходят только админы» (по умолчанию включён);
      2) MAINTENANCE_MODE — аварийные техработы (тоже только для админов).
    Проверка серверная: даже если клиент нарисует себе кнопки, игровые
    эндпоинты ответят 403.
    """
    admin = is_admin_id(user_id, ADMIN_IDS)
    if ADMIN_ONLY_APP and not admin:
        return False
    if not MAINTENANCE_MODE:
        return True
    return admin


def require_play_access(user) -> int:
    """Единая проверка входа в игру. Возвращает user_id, иначе — HTTPException."""
    user_id = _safe_int(user.get("id"), None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    if not can_play(user_id):
        raise HTTPException(status_code=403, detail=_access_denied_message(user_id))
    return user_id


def _access_denied_message(user_id) -> str:
    """Почему не пускаем: админ-онли понятнее, чем «техработы»."""
    if ADMIN_ONLY_APP and not is_admin_id(user_id, ADMIN_IDS):
        return ADMIN_ONLY_MESSAGE
    return MAINTENANCE_MESSAGE


# ==================== СУНДУКИ ====================
CHEST_COOLDOWN = 18000              # обычный сундук: 5 часов
CHEST_MIN_COINS, CHEST_MAX_COINS = 10, 30      # обычный сундук: 10-30 коинов
CHEST_KEY_CHANCE = 0.20             # 20% на золотой ключ вместо коинов
GOLD_MIN_COINS, GOLD_MAX_COINS = 60, 180       # золотой сундук (открывается ключом)
GOLD_KEY_BACK_CHANCE = 0.10         # 10% вернуть ключ
# ОБЫЧНАЯ КАПСУЛА ИЗ СУНДУКА (задача 22): отдельный бросок, который идёт В ДОБАВОК
# к коинам/ключу, а не вместо них. Шансы: золотой сундук — 10%, обычный — 0.1%.
CHEST_CAPSULE_ITEM = "capsule"
CHEST_CAPSULE_AMOUNT = 1
CHEST_CAPSULE_CHANCE_NORMAL = 0.001   # 0.1 %  (один шанс из тысячи)
CHEST_CAPSULE_CHANCE_GOLD = 0.10      # 10 %
# Колонки chest_claims для капсулы. Появились в миграции
# 20261108000000_chest_capsule_reward.sql. Пока её не применили, капсула всё
# равно не теряется: она держится в памяти процесса до кнопки «Забрать».
CHEST_ITEM_COLUMN = "reward_item"
CHEST_ITEM_AMOUNT_COLUMN = "reward_item_amount"
# Награда сундука сначала только разыгрывается: баланс меняет /api/claim_chest,
# когда игрок нажал «Забрать» (иначе коины приходили до анимации рулетки).
CHEST_CLAIMS_TABLE = "chest_claims"
CHEST_CLAIMS_GENERIC = "Не удалось открыть сундук, попробуйте ещё раз"
CHEST_CLAIMS_UNAVAILABLE = "Сундуки обновляются, попробуйте чуть позже"
CHEST_CLAIMS_MIGRATION_FILE = "supabase/migrations/20261030000000_capsule_stock_promo_types.sql"


def _chest_claims_detail(exc: Exception) -> str:
    """Текст ошибки для игрока + честный лог для админа.

    Раньше ЛЮБАЯ ошибка insert/update отдавала в мини-аппку «примените миграцию
    chest_claims» — сообщение про миграцию видел и админ, у которого всё уже
    применено. Теперь инструкция уходит в лог, а игрок получает нейтральный текст.
    """
    message = f"{type(exc).__name__}: {exc}"
    if any(marker in message for marker in
           ("chest_claims", "PGRST204", "PGRST205", "does not exist", "schema cache")):
        logger.error("Таблица %s недоступна. Примените %s и выполните NOTIFY pgrst, 'reload schema'. Ошибка: %s",
                     CHEST_CLAIMS_TABLE, CHEST_CLAIMS_MIGRATION_FILE, message)
        return CHEST_CLAIMS_UNAVAILABLE
    logger.warning("Сбой сундуков: %s", message)
    return CHEST_CLAIMS_GENERIC


@app.get("/api/chest_ready")
async def chest_ready():
    """Готова ли таблица сундуков (диагностика для бота и админа).

    Клиент получает «Сундуки обновляются, попробуйте чуть позже», когда
    chest_claims недоступна: не применена миграция или в PostgREST устарел кэш
    схемы. Эта ручка отвечает одной строкой, что именно не так, чтобы не гадать
    по логам Render.
    """
    def _probe():
        response = (
            supabase.table(CHEST_CLAIMS_TABLE)
            .select("id")
            .limit(1)
            .execute()
        )
        return response.data or []

    def _probe_rpc():
        supabase.rpc("chest_claim_pending", {"p_user_id": "__probe__"}).execute()
        return True

    try:
        rows = await asyncio.to_thread(_probe)
    except Exception as exc:
        # Сундуки при этом всё равно работают: награда ждёт «Забрать»
        # в памяти процесса, но не начисляется при открытии (см. open_chest).
        print(f"ОШИБКА СУНДУКА: таблица {CHEST_CLAIMS_TABLE} недоступна: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return JSONResponse(status_code=200, content={
            "ok": False,
            "table": CHEST_CLAIMS_TABLE,
            "detail": _chest_claims_detail(exc),
            "error": f"{type(exc).__name__}: {exc}"[:400],
            "migration": CHEST_CLAIMS_MIGRATION_FILE,
            "rpc_migration": "supabase/migrations/20261102000000_stock_and_chest_rpc.sql",
            "fallback": "награда ждёт «Забрать» в памяти сервера (пропадает при перезапуске)",
            "hint": "Примените миграции и выполните NOTIFY pgrst, 'reload schema'",
        })

    try:
        rpc_ready = await asyncio.to_thread(_probe_rpc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("RPC сундуков недоступны: %s", exc)
        rpc_ready = False
    return {"ok": True, "table": CHEST_CLAIMS_TABLE, "rows_preview": len(rows),
            "rpc_ready": rpc_ready,
            "rpc_missing_hint": None if rpc_ready else
            "примените supabase/migrations/20261102000000_stock_and_chest_rpc.sql"}

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
# Задача 25: ставка до 10 000 коинов и до 50 капсул за один апгрейд.
# Раньше «Слишком большой апгрейд за один раз» вылетало уже на x20 при ставке
# 5 000 — теперь множители и лимиты согласованы между собой.
UPGRADE_MAX_BET = 10000         # максимум коинов на левой панели
UPGRADE_MAX_CAPSULES = 50       # максимум капсул за один апгрейд (лимит инвентаря)
UPGRADE_MIN_CHANCE = 0.5        # ниже 0.5% цель считается недостижимой
UPGRADE_MAX_CHANCE = 95.0       # 100% и выше не выдаём
# Потолок выплаты — страховка от абсурдных значений в запросе, а не ограничение
# игры: 10 000 * x50 = 500 000 проходит свободно.
UPGRADE_MAX_REWARD = 2_000_000
# Множители апгрейда (задача 25): x4 и x8 убраны, добавлены «почти монетка»
# x1.12/x1.35/x1.55/x1.75 и редкие крупные x20/x50.
UPGRADE_COIN_MULTIPLIERS = [1.12, 1.35, 1.55, 1.75, 20, 50]
# Шанс = 90 / X. Одна формула на всё: и на кнопки x1.12…x50, и на «свой»
# множитель из ползунка. Честный шанс был бы 100 / X, то есть 90 / X — это те же
# «сливает чуть чаще» 10%, но ЗАМЕТНО понятнее таблицы: игрок может проверить
# любое число на калькуляторе. Пример: x1.75 → 90 / 1.75 = 51.43%.
UPGRADE_CHANCE_NUMERATOR = 90.0
UPGRADE_MIN_MULTIPLIER = 1.12
UPGRADE_MAX_MULTIPLIER = 50.0
UPGRADE_QUICK_CHANCES = [35, 55, 75]
# Стоимость капсул для апгрейда. Совпадает с shop_catalog.SHOP_ITEMS
# (+ LEGACY_ITEMS), поэтому в игру можно ставить ЛЮБУЮ капсулу из инвентаря.
UPGRADE_ITEM_PRICES = {"capsule": 100, "uniq_capsule": 500, "ruby": 1000,
                       "diamond": 5000, "galactic": 10000,
                       "malachite": 3000, "emerald": 5000}
UPGRADE_SOURCES = ("coin",) + tuple(UPGRADE_ITEM_PRICES)
UPGRADE_TARGETS = ("coin",) + tuple(UPGRADE_ITEM_PRICES)
# В апгрейде пока играют только админы — шанс честный. Когда откроем для всех,
# реальный шанс = визуальный / этот делитель (то есть x2 = 25%).
UPGRADE_NON_ADMIN_DIVIDER = 2
# Апгрейд пока закрыт для всех, кроме ADMIN_IDS (задача 21): техработы сняты
# и играют все, но апгрейд — отдельная проверка, от MAINTENANCE_MODE не зависит.
# Когда решишь открыть апгрейд всем — поставь здесь False.
UPGRADE_ADMIN_ONLY = True
UPGRADE_ADMIN_ONLY_MESSAGE = "Апгрейд доступен только администраторам"

# ДОМ-ЭДЖ ПО БАЛАНСУ (задача 18): с баланса 1000 коинов и выше играем чаще
# в минус — реальный шанс = расчётный * этот множитель (50% → 40%).
# Клиент считает тот же множитель по своему балансу (UPGRADE_HOUSE_EDGE_* в
# miniapp/app.js), поэтому зелёная зона круга и вердикт сервера совпадают.
# Задача 26: дом-эдж больше не зависит от баланса — он «вшит» в таблицу выше
# (там шанс уже ниже честного 1/множитель). Оставляем 1.0 по умолчанию, чтобы
# круг не врал; админ может подкрутить значение в /admin (game_settings).
UPGRADE_HOUSE_EDGE_MIN_BALANCE = 1000
UPGRADE_HOUSE_EDGE_FACTOR = 1.0

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


# ==================== ПОНЯТНЫЕ ОШИБКИ БАЗЫ ====================
# Частые поломки Supabase: в таблице users нет колонки (inventory, last_chest,
# daily_buys, …) или нет самой таблицы. PostgREST отвечает PGRST204/PGRST205/
# 42703 — раньше игрок видел «Внутренняя ошибка», а админ не понимал, что чинить.
_USERS_MIGRATION_HINT = ("если колонки нет в базе — сверьте таблицу users с "
                         "supabase/migrations и добавьте недостающую колонку")


def _db_error_detail(exc: Exception, fallback: str = "База временно недоступна") -> str:
    """Человеческий текст ошибки БД для фронтенда (его показывает tg.showAlert).

    Возвращает готовый `detail` для HTTPException: без SQL-простыней, но с
    конкретикой — какой колонки/таблицы не хватает и что применить.
    """
    message = f"{type(exc).__name__}: {exc}"
    lower = message.lower()

    # 1) Нет таблицы / устарел кэш схемы PostgREST.
    if "pgrst205" in lower or "42p01" in lower or "could not find the table" in lower:
        return ("Таблица users не найдена в базе (или устарел кэш PostgREST). "
                "Проверьте SUPABASE_URL и выполните NOTIFY pgrst, 'reload schema'")

    # 2) Нет колонки: PostgREST: "Could not find the 'inventory' column of 'users'".
    column = None
    if "pgrst204" in lower or "42703" in lower or "column" in lower:
        import re as _re
        found = _re.search(r"'([a-z_][a-z0-9_]*)'\s+column", lower) or \
                _re.search(r"column\s+'?\"?([a-z_][a-z0-9_]*)", lower)
        if found:
            column = found.group(1)
        where = f"колонки «{column}» " if column else ""
        return (f"В базе не хватает {where}в таблице users. "
                "Примените миграции из supabase/migrations (или добавьте колонку), "
                "затем повторите — данные игрока не потеряны")

    # 3) Права: RLS без политики.
    if "row-level security" in lower or "42501" in lower:
        return ("База отклонила запись (RLS без политики). Примените "
                "supabase/migrations/20261107000000_open_launch_promo_rls_and_stock_reset.sql "
                "или задайте SUPABASE_SERVICE_KEY")

    # 4) Сеть/таймаут.
    if "timeout" in lower or "connection" in lower or "network" in lower:
        return f"{fallback}: база не ответила вовремя, попробуйте ещё раз"

    if "pgrst202" in lower or "does not exist" in lower or "schema cache" in lower:
        return ("База не нашла нужную функцию или таблицу (устарел кэш PostgREST). "
                "Выполните NOTIFY pgrst, 'reload schema' и повторите")

    return fallback


# Последняя ошибка чтения users: _find_user_rows её не пробрасывает (иначе любой
# сбой базы ломал бы профиль целиком), но обработчики должны показать причину
# игроку, а не сухое «профиль не найден».
_LAST_USER_READ_ERROR = None


def _last_user_read_error():
    return _LAST_USER_READ_ERROR


def _find_user_rows(user_id):
    """Ищем строку и по int, и по строке: в старой базе user_id мог сохраниться текстом."""
    global _LAST_USER_READ_ERROR
    _LAST_USER_READ_ERROR = None      # ошибка должна относиться именно к этому чтению
    uid = _safe_int(user_id, None)
    if uid is None:
        return []
    rows = []
    for key in (uid, str(uid)):
        try:
            res = supabase.table("users").select("*").eq("user_id", key).execute()
        except Exception as exc:  # noqa: BLE001
            _LAST_USER_READ_ERROR = exc
            logger.error("Не удалось прочитать users по user_id=%s: %s", key, exc)
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
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось обновить users по user_id=%s (%s): %s",
                         key, ", ".join(sorted(payload)), exc)
            continue
        if updated.data:
            return True
    return False


def _upgrade_economy_config() -> dict:
    """Настройки апгрейда для клиента: одна правда о шансах.

    Клиент рисует ровно эти числа (таблица «множитель → шанс», дом-эдж и
    админский множитель), поэтому подпись на круге и вердикт сервера не могут
    разойтись. Вынесено в отдельную функцию, чтобы /api/health и /api/user
    отдавали один и тот же объект.
    """
    return {
        "max_bet": UPGRADE_MAX_BET,
        "max_capsules": UPGRADE_MAX_CAPSULES,
        "multipliers": UPGRADE_COIN_MULTIPLIERS,
        # Шанс = numerator / X (одинаково для кнопок и для «своего» X).
        "chance_numerator": UPGRADE_CHANCE_NUMERATOR,
        "multiplier_chances": {str(key): upgrade_multiplier_chance(key)
                               for key in UPGRADE_COIN_MULTIPLIERS},
        "mult_min": UPGRADE_MIN_MULTIPLIER,
        "mult_max": UPGRADE_MAX_MULTIPLIER,
        "item_prices": UPGRADE_ITEM_PRICES,
        "sources": list(UPGRADE_SOURCES),
        "house_edge": game_setting_float("upgrade_house_edge", UPGRADE_HOUSE_EDGE_FACTOR),
        "chance_factor": game_setting_float("upgrade_chance_factor", 1.0),
        "admin_only": UPGRADE_ADMIN_ONLY,
    }


def _user_payload(row, is_admin, user_id=None):
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
    uid = user_id if user_id is not None else row.get("user_id")
    allowed = can_play(uid) if uid is not None else bool(is_admin)
    return {
        "ok": True,
        "coins": _safe_int(row.get("coins"), 0),
        "keys": _safe_int(row.get("keys"), 0),
        "inventory": parse_inventory(row.get("inventory")),
        "last_chest": _safe_int(row.get("last_chest"), 0),
        "is_admin": is_admin,
        # Техработы: приложение показывает экран-заглушку всем, у кого нет доступа.
        "maintenance": MAINTENANCE_MODE or ADMIN_ONLY_APP,
        "maintenance_message": ("" if allowed else _access_denied_message(uid or 0)),
        "admin_only": ADMIN_ONLY_APP,
        "can_play": bool(allowed),
        "created_at": created_text,
        "days_in_game": _days_in_game(created_at),
        # Пассивный доход: клиент считает «в час» по своему инвентарю этими ставками.
        "passive_rates": PASSIVE_INCOME_RATES,
        "channel_url": CHANNEL_URL,
        "subscription_bonus": SUBSCRIPTION_BONUS,
        # Доступ и экономика: клиенту нужны те же числа, что играет сервер
        # (шансы апгрейда, лимиты, режим «только админы»).
        "access": {
            "admin_only_app": ADMIN_ONLY_APP,
            "admins": len(ADMIN_IDS),
            "message": ADMIN_ONLY_MESSAGE if ADMIN_ONLY_APP else "",
        },
        "economy": {"upgrade": _upgrade_economy_config()},
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


# ==================== КРИПТОСТОЙКИЙ ГЕНЕРАТОР ====================
# Игровой рандом не должен зависеть от Mersenne Twister: его состояние можно
# восстановить по нескольким выдачам. Для ставок, апгрейдов и сундуков берём
# secrets (os.urandom), а random оставляем только для «декора» — имена ботов,
# цвета, паузы между ботами.
def secure_float(lo: float = 0.0, hi: float = 1.0) -> float:
    """Равномерное число [lo, hi) из криптографического источника."""
    span = float(hi) - float(lo)
    if span <= 0:
        return float(lo)
    # 53 бита — ровно столько, сколько помещается в мантиссу double, поэтому
    # распределение остаётся равномерным без «дырок» на краях.
    return float(lo) + span * (secrets.randbits(53) / float(1 << 53))


def secure_int(lo: int, hi: int) -> int:
    """Целое [lo, hi] включительно, без смещения в сторону меньших чисел."""
    lo, hi = int(lo), int(hi)
    if hi <= lo:
        return lo
    return lo + secrets.randbelow(hi - lo + 1)


def secure_choice(seq):
    if not seq:
        return None
    return seq[secrets.randbelow(len(seq))]


def roll_percent() -> float:
    """Бросок 0..100 с двумя знаками — ровно то, что сравнивается с шансом."""
    return min(100.0, max(0.0, round(secure_float(0.0, 100.0), 2)))


def roll_unit() -> float:
    """Бросок [0, 1): для шансов вида «10% капсулы из сундука»."""
    return secure_float(0.0, 1.0)


# ==================== НАСТРОЙКИ ИГРЫ (админ меняет шансы) ====================
# Таблица game_settings (key/value) — единственный способ менять коэффициенты
# без деплоя: бот пишет, API читает с коротким кэшем.
GAME_SETTINGS_TABLE = "game_settings"
GAME_SETTINGS_TTL = 30.0
_game_settings_cache = {"at": 0.0, "values": {}}


def _load_game_settings(force: bool = False) -> dict:
    now = time.time()
    if not force and (now - _game_settings_cache["at"]) < GAME_SETTINGS_TTL:
        return _game_settings_cache["values"]
    try:
        res = supabase.table(GAME_SETTINGS_TABLE).select("key, value").execute()
        values = {}
        for row in (res.data or []):
            key = str(row.get("key") or "").strip()
            if key:
                values[key] = row.get("value")
    except Exception:  # таблицы может не быть — играем на дефолтах
        values = _game_settings_cache["values"]
    _game_settings_cache.update({"at": now, "values": values})
    return values


def game_setting_float(key: str, default: float) -> float:
    raw = _load_game_settings().get(key)
    if raw is None or raw == "":
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def game_setting_set(key: str, value) -> bool:
    """Запись настройки. None/'' — удалить строку (вернуться к дефолту)."""
    key = str(key or "").strip()
    if not key:
        return False
    try:
        if value is None or value == "":
            supabase.table(GAME_SETTINGS_TABLE).delete().eq("key", key).execute()
        else:
            supabase.table(GAME_SETTINGS_TABLE).upsert(
                {"key": key, "value": str(value)}, on_conflict="key").execute()
        _game_settings_cache["at"] = 0.0        # следующий вызов перечитает
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось записать настройку %s: %s", key, exc)
        return False


# ==================== IDEMPOTENCY ====================
# Клиент присылает request_id (uuid). Один и тот же ключ = один и тот же
# результат: повтор после таймаута не спишет деньги второй раз.
def _parse_timestamp(value) -> Optional[float]:
    """Строка/дата из базы → unix-время. Мусор → None (а не исключение)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Supabase иногда отдаёт секунды, иногда миллисекунды.
        number = float(value)
        return number / 1000.0 if number > 1e11 else number
    if isinstance(value, datetime.datetime):
        stamp = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            stamp = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def _idempotency_key(user_id, request_id, endpoint: str) -> Optional[str]:
    raw = str(request_id or "").strip()
    if not raw:
        return None
    if len(raw) > 80:
        raw = raw[:80]
    return f"{endpoint}:{user_id}:{raw}"


def idempotency_lookup(key: Optional[str]):
    """Сохранённый ответ или None. Ошибка базы не должна ломать оплату."""
    if not key:
        return None
    try:
        res = (supabase.table(IDEMPOTENCY_TABLE).select("response, created_at")
               .eq("key", key).limit(1).execute())
    except Exception:
        return None
    if not res.data:
        return None
    row = res.data[0]
    created = row.get("created_at")
    # Устаревший ключ считаем недействительным: иначе «через сутки» игрок
    # получил бы старый ответ вместо новой покупки.
    stamp = _parse_timestamp(created)
    if stamp is not None and time.time() - stamp > IDEMPOTENCY_TTL:
        return None
    payload = row.get("response")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    return payload if isinstance(payload, dict) else None


def idempotency_store(key: Optional[str], user_id, endpoint: str, payload: dict) -> None:
    if not key or not isinstance(payload, dict):
        return
    try:
        supabase.table(IDEMPOTENCY_TABLE).upsert({
            "key": key, "user_id": int(user_id), "endpoint": endpoint,
            "response": json.dumps(payload, ensure_ascii=False),
            "created_at": _now_iso(),
        }, on_conflict="key").execute()
    except Exception as exc:  # noqa: BLE001 — не роняем уже совершённую операцию
        logger.warning("Не удалось сохранить idempotency-ключ %s: %s", key, exc)


def _enforce_bet_limit(user_id, total_bet):
    """Строгая серверная проверка общей ставки: > MAX_BET запрещено всем, кроме админов."""
    if _is_admin_user(user_id):
        return
    if int(total_bet) > MAX_BET:
        raise HTTPException(status_code=400, detail=f"Максимальная ставка {MAX_BET} 🪙")


# === АРЕНА ===
# Две команды командной арены. Цвета — те же, что у красной/синей кнопки в UI.
ARENA_TEAMS = {
    "red": {"name": "Красные", "color": "#ff3b30"},
    "blue": {"name": "Синие", "color": "#007aff"},
}


def _new_arena_state(mode: str = "normal") -> dict:
    return {
        "status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
        "last_update": time.time(), "last_bot_added": time.time(),
        "target_bots": _random_arena_bot_count(), "is_paid": False,
        "payout_attempt_at": 0, "round_id": str(int(time.time())), "whales_added": 0,
        "roll_data": None, "rolling_started_at": None, "whale_decided": False,
        "whale_join": False, "whale_trigger_at": None,
        # mode: 'normal' — сектор на каждого игрока, 'team' — красные против синих
        "mode": mode if mode in ("normal", "team") else "normal",
        "winner_team": None, "payouts": [], "paid_users": [],
    }


game_state = _new_arena_state()
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

    team = secure_choice(["red", "blue"]) if game_state.get("mode") == "team" else None
    game_state["players"].append({
        "id": f"bot_{random.randint(10000, 99999)}", "name": name, "avatar": "",
        "bet": bet, "color": ARENA_TEAMS[team]["color"] if team else color,
        "is_bot": True, "team": team,
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


def _arena_mode() -> str:
    """'team' — красные против синих, 'normal' — сектор на каждого игрока."""
    return "team" if game_state.get("mode") == "team" else "normal"


def _hockey_teams() -> list:
    """Сводка по командам: сумма ставок и доля круга в процентах."""
    totals = {"red": 0, "blue": 0}
    for player in game_state["players"]:
        team = player.get("team")
        if team in totals:
            totals[team] += max(0, int(player.get("bet", 0)))
    total = sum(totals.values())
    if total <= 0:
        return []
    return [
        {"id": team, "name": ARENA_TEAMS[team]["name"], "color": ARENA_TEAMS[team]["color"],
         "bet": totals[team], "percent": round(totals[team] / total * 100.0, 4), "is_team": True}
        for team in ("red", "blue") if totals[team] > 0
    ]


def _hockey_sectors() -> list:
    """Сектора круга ровно в том порядке, в котором по ним ходит бросок.

    В обычном режиме — по игроку на сектор (как раньше), в командном — два
    сектора, чьи размеры пропорциональны ставкам команд.
    """
    if _arena_mode() == "team" and any(player.get("team") for player in game_state["players"]):
        teams = _hockey_teams()
        if teams:
            return teams
    return [dict(player) for player in game_state["players"]]


def _hockey_winner_at_x(x):
    """Resolve a final ball x-coordinate to the bet-weighted arena sector."""
    sectors = _hockey_sectors()
    total_bets = sum(max(0, int(sector.get("bet", 0))) for sector in sectors)
    if total_bets <= 0:
        return None

    current_x = 0.0
    for sector in sectors:
        sector_width = max(0, int(sector.get("bet", 0))) / total_bets * 100.0
        if current_x <= x <= current_x + sector_width + 0.0001:
            return sector
        current_x += sector_width
    return sectors[-1] if sectors else None


def _hockey_payout_plan() -> list:
    """Кому и сколько платим. Считается один раз на раунд.

    Обычный режим: весь банк одному победителю (как было).
    Командный режим: банк делится между реальными игроками победившей команды
    пропорционально их ставкам. Доля ботов из банка не выплачивается — это и
    есть дом-эдж арены.
    """
    plan = game_state.get("payouts") or []
    if plan:
        return plan
    bank = int(game_state.get("bank") or 0)
    winner_team = game_state.get("winner_team")
    if winner_team:
        team_players = [p for p in game_state["players"] if p.get("team") == winner_team]
        team_total = sum(max(0, int(p.get("bet", 0))) for p in team_players)
        if team_total <= 0:
            return []
        for player in team_players:
            if player.get("is_bot"):
                continue
            try:
                numeric_id = int(player["id"])
            except (TypeError, ValueError):
                continue
            amount = int(bank * max(0, int(player.get("bet", 0))) / team_total)
            if amount > 0:
                plan.append({"user_id": numeric_id, "amount": amount, "name": player.get("name")})
    else:
        winner = game_state.get("winner")
        if winner and not winner.get("is_bot"):
            try:
                numeric_id = int(winner["id"])
            except (TypeError, ValueError):
                numeric_id = None
            if numeric_id is not None:
                plan.append({"user_id": numeric_id, "amount": bank, "name": winner.get("name")})
    game_state["payouts"] = plan
    return plan


def _payout_hockey_winner():
    """Выплатить банк победителю (или победившей команде), повторяя при сбоях базы."""
    if game_state["is_paid"]:
        return
    plan = _hockey_payout_plan()
    if not plan:
        # Победили боты или банка нет — платить некому, раунд считаем закрытым.
        game_state["is_paid"] = True
        return

    now = time.time()
    if now - game_state.get("payout_attempt_at", 0) < 2:
        return
    game_state["payout_attempt_at"] = now

    try:
        for entry in plan:
            uid = entry["user_id"]
            if uid in (game_state.get("paid_users") or []):
                continue
            res = supabase.table("users").select("coins").eq("user_id", uid).execute()
            if not res.data:
                raise RuntimeError(f"Победитель Арены user_id={uid} не найден в users")
            new_coins = int(res.data[0].get("coins", 0)) + int(entry["amount"])
            updated = supabase.table("users").update({"coins": new_coins}).eq(
                "user_id", uid).select("user_id").execute()
            if not updated.data:
                raise RuntimeError(f"Supabase не подтвердил выплату user_id={uid}")
            game_state.setdefault("paid_users", []).append(uid)
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
    game_state["winner_team"] = None
    if not game_state["players"] or game_state["bank"] <= 0 or "final_x" not in roll_data:
        game_state["winner"] = None
    else:
        sector = _hockey_winner_at_x(float(roll_data["final_x"]))
        if sector is not None and sector.get("is_team"):
            # Командный режим: банк забирает команда, а «победитель» для модалки —
            # самый крупный реальный игрок этой команды.
            game_state["winner_team"] = sector["id"]
            team_players = [p for p in game_state["players"] if p.get("team") == sector["id"]]
            human_players = [p for p in team_players if not p.get("is_bot")] or team_players
            game_state["winner"] = (max(human_players, key=lambda p: int(p.get("bet", 0)))
                                    if human_players else None)
        else:
            game_state["winner"] = sector
    game_state["payouts"] = []
    game_state["paid_users"] = []
    _hockey_payout_plan()

    game_state["status"] = "finished"
    game_state["last_update"] = time.time()
    if game_state["winner"]:
        history.insert(0, {
            "round_id": game_state["round_id"],
            "winner_name": (f"{ARENA_TEAMS[game_state['winner_team']]['name']} · "
                            f"{game_state['winner']['name']}" if game_state.get("winner_team")
                            else game_state["winner"]["name"]),
            "winner_team": game_state.get("winner_team"),
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


@app.on_event("startup")
async def init_capsule_stock():
    """Первый запуск: строки глобального тиража капсул создаются сами.

    ВАЖНО про скорость пробуждения: FastAPI выполняет startup-обработчики ДО
    того, как начнёт принимать запросы. Раньше здесь стоял прямой запрос к
    Supabase, и каждый холодный старт Render дополнительно ждал базу — то есть
    игрок дольше смотрел на экран «SERVICE WAKING UP». Теперь подготовка идёт
    фоновой задачей: порт открывается сразу, `uptime` считается с этого момента.
    """
    async def _prepare():
        try:
            stock = await asyncio.to_thread(shop_stock_snapshot, supabase)
        except Exception:
            logger.exception("Не удалось инициализировать сток капсул")
            return
        if stock is None:
            logger.warning(
                "Таблица capsule_stock недоступна — примените миграцию "
                "20261030000000_capsule_stock_promo_types.sql в Supabase"
            )
            return
        logger.info("Сток капсул: %s", stock)

    asyncio.create_task(_prepare())


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


class BuyItem(BaseModel):
    initData: str
    item_id: str
    price: Optional[int] = None
    # Ключ идемпотентности: повторный запрос с тем же id вернёт сохранённый ответ.
    request_id: Optional[str] = None


class ShopCatalogRequest(BaseModel): initData: str


class PlaceBet(BaseModel):
    initData: str
    amount: int
    # 'red'/'blue' — командная арена (задача 25). В обычном режиме игнорируется.
    team: Optional[str] = None
    request_id: Optional[str] = None


class FinishHockey(BaseModel): initData: str; x: float; y: float


class ArenaModeRequest(BaseModel):
    initData: str
    mode: str = "normal"


class CrashBet(BaseModel): initData: str; amount: int


class CrashCashout(BaseModel): initData: str


class MinesStart(BaseModel): initData: str; amount: int; mines: int = 1


class MinesOpen(BaseModel): initData: str; cell: int


class MinesCashout(BaseModel): initData: str


class OpenChestRequest(BaseModel):
    initData: str
    chest_type: str = "normal"
    request_id: Optional[str] = None


class ClaimChestRequest(BaseModel):
    initData: str
    # int — строка в chest_claims; str «mem-…» — награда ждёт в памяти процесса
    # (когда таблица недоступна). См. _pending_claim_put.
    claim_id: Optional[Union[int, str]] = None


class UpgradeLogRequest(BaseModel): initData: str


class UpgradeAck(BaseModel):
    initData: str
    log_id: Optional[int] = None


class UpgradeAction(BaseModel):
    initData: str
    request_id: Optional[str] = None
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
    # Круглая арена: клиент рисует сектора ровно в этом порядке (доли круга).
    response["sectors"] = _hockey_sectors()
    response["teams"] = _hockey_teams()
    response["mode"] = _arena_mode()
    response["payouts"] = game_state.get("payouts") or []
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
            previous_mode = game_state.get("mode", "normal")
            game_state = _new_arena_state(previous_mode)
            game_state["last_update"] = now
            game_state["last_bot_added"] = now
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
    require_play_access(user)  # техработы: во время них играют только админы
    user_id = str(user["id"])

    check_ban(user_id)

    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше нуля")
    if (game_state["status"] != "waiting"
            or time.time() - game_state["last_update"] >= 15):
        raise HTTPException(status_code=400, detail="Ставки закрыты")
    # Команда (только в командном режиме): 'red' / 'blue'. В обычном игнорируется.
    team = (data.team or "").strip().lower() or None
    if team not in (None, "red", "blue"):
        raise HTTPException(status_code=400, detail="Неизвестная команда")
    if _arena_mode() != "team":
        team = None

    existing_player = next(
        (p for p in game_state["players"] if p["id"] == user_id and not p.get("is_bot")),
        None,
    )
    # Нельзя ставить за обе команды в одном раунде: иначе игрок «страхуется»
    # и выигрывает всегда. Отказ понятный, чтобы UI мог подсветить кнопку.
    if (existing_player is not None and team
            and existing_player.get("team") and existing_player.get("team") != team):
        blocked = ARENA_TEAMS.get(existing_player["team"], {}).get("name", "другую команду")
        raise HTTPException(status_code=400, detail=f"Вы уже ставите за {blocked} — дождитесь конца раунда")
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
            "color": ARENA_TEAMS[team]["color"] if team else player_color,
            "is_bot": False, "team": team,
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


@app.post("/api/arena_mode")
async def arena_mode(data: ArenaModeRequest):
    """Переключение «Обычная арена» / «Командная арена».

    Меняем режим только в фазе ожидания и только пока в раунде нет живых
    ставок: иначе у игрока уже стоит сектор по старой модели. Боты получают
    команду автоматически при следующем входе в круг.
    """
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)
    mode = (data.mode or "").strip().lower()
    if mode not in ("normal", "team"):
        raise HTTPException(status_code=400, detail="Неизвестный режим арены")
    if game_state["status"] != "waiting":
        raise HTTPException(status_code=400, detail="Режим переключим после этого раунда")
    if any(not p.get("is_bot") for p in game_state["players"]):
        raise HTTPException(status_code=400, detail="Режим переключим после этого раунда")
    game_state["mode"] = mode
    # Уже набежавших ботов раскладываем по командам, чтобы круг не был пустым.
    for player in game_state["players"]:
        if mode == "team":
            player["team"] = player.get("team") or secure_choice(["red", "blue"])
            player["color"] = ARENA_TEAMS[player["team"]]["color"]
        else:
            player["team"] = None
    _update_hockey_percentages()
    return {"ok": True, "mode": mode, "teams": _hockey_teams()}


# ==================== РАКЕТА (CRASH) ====================
def get_crash_point_by_chance():
    """Мгновенные краши x1.00-x1.06 выпадают редко (12%), а не в трети раундов."""
    rv = roll_unit()
    if rv < 0.12:
        return secure_float(1.00, 1.06)
    elif rv < 0.46:
        return secure_float(1.06, 2.0)
    elif rv < 0.85:
        return secure_float(2.0, 4.0)
    elif rv < 0.99:
        return secure_float(4.0, 50.0)
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
    require_play_access(user)  # техработы: во время них играют только админы
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
    require_play_access(user)  # техработы: во время них играют только админы
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
    require_play_access(user)  # техработы: во время них играют только админы
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
    require_play_access(user)  # техработы: во время них играют только админы
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
        if roll_unit() < game["first_click_chance"]:
            positions.add(cell)
        need = game["mines"] - len(positions)
        pool = [index for index in range(MINES_CELLS) if index not in positions]
        # Своя выборка без повторов на secrets: random.sample() предсказуем.
        for _ in range(need):
            if not pool:
                break
            picked = pool.pop(secrets.randbelow(len(pool)))
            positions.add(picked)
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
    require_play_access(user)  # техработы: во время них играют только админы
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
    except Exception as exc:  # noqa: BLE001 — детали нужны и игроку, и админу
        logger.exception("Ошибка чтения профиля user_id=%s", user_id)
        raise HTTPException(status_code=503, detail=_db_error_detail(
            exc, "База временно недоступна, попробуйте ещё раз")) from exc

    if not rows:
        # Создаём профиль только если строки действительно нет ни в одном варианте id.
        # inventory пишем строкой JSON: так его хранит и бот, и магазин.
        new_user = {"user_id": user_id, "coins": 0, "keys": 0,
                    "username": user.get("username", f"id{user_id}"),
                    "last_chest": 0, "chat_id": 0, "inventory": "[]"}
        create_error = None
        try:
            supabase.table("users").insert(new_user).execute()
        except Exception as exc:  # noqa: BLE001
            create_error = exc
            logger.exception("Не удалось создать профиль user_id=%s", user_id)
        rows = _find_user_rows(user_id)
        if not rows:
            detail = "Профиль не найден в базе, попробуйте позже"
            cause = create_error or _last_user_read_error()
            if cause is not None:
                detail = _db_error_detail(cause, detail)
            raise HTTPException(status_code=503, detail=detail)

    # Если в базе оказалось несколько строк (старые дубли), берём ту, где больше коинов —
    # иначе игрок видит 0, хотя в «кармане» бота лежат его деньги.
    row = max(rows, key=lambda item: _safe_int(item.get("coins"), 0))
    # «0 дней в игре» лечится здесь же: если у старого профиля нет created_at,
    # ставим текущий момент, и с этого дня счётчик начнёт расти. Раньше NULL
    # в базе навсегда оставлял игрока с нулём.
    if not row.get("created_at"):
        backfill = _now_iso()
        if _db_update_user(user_id, created_at=backfill):
            row["created_at"] = backfill
    if len(rows) > 1:
        logger.warning("Дубли профиля user_id=%s: %s", user_id,
                       [(_safe_int(item.get("id"), None), _safe_int(item.get("coins"), 0)) for item in rows])
    return _user_payload(row, is_admin, user_id)


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
        "maintenance": MAINTENANCE_MODE,
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


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ChestUnavailable(Exception):
    """chest_claims недоступна: таблицы/функции нет, RLS или база отказала.

    Раньше такая ошибка превращалась в 503 «Сундуки обновляются, попробуйте
    чуть позже», и сундук нельзя было открыть вообще. Теперь награда не
    теряется и не начисляется сразу: она ждёт кнопку «Забрать» в памяти
    процесса (см. _pending_claim_put).
    """


# Признаки «таблица chest_claims или функция chest_claim_* недоступна».
_CHEST_UNAVAILABLE_MARKERS = (
    "chest_claims", "chest_claim_", "PGRST202", "PGRST204", "PGRST205", "42P01", "42501",
    "row-level security", "does not exist", "schema cache",
    "Could not find the table", "Could not find the function", "permission denied",
)
_RPC_MISSING_MARKERS = (
    "PGRST202", "PGRST205", "404", "Could not find the function",
    "Could not find the table", "does not exist", "schema cache",
)


def _log_chest_error(context: str, exc: Exception, user_id=None) -> None:
    """Одна строка в консоль Render + в лог: по ней видно, что именно сломалось."""
    text = f"{type(exc).__name__}: {exc}"
    print(f"ОШИБКА СУНДУКА: {context} user_id={user_id}: {text}", flush=True)
    logger.error("ОШИБКА СУНДУКА: %s user_id=%s: %s", context, user_id, text)


def _chest_unavailable(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}"
    return any(marker in message for marker in _CHEST_UNAVAILABLE_MARKERS)


def _rpc_missing(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}"
    return any(marker in message for marker in _RPC_MISSING_MARKERS)


# ==================== СУНДУКИ БЕЗ ТАБЛИЦЫ ====================
# Требование игрока: баланс меняется ТОЛЬКО по кнопке «Забрать». Если
# chest_claims недоступна (нет таблицы / RLS), награда не начисляется сразу,
# а ждёт в памяти процесса под строковым id вида «mem-…».
# Минус: при перезапуске Render такая награда пропадёт — поэтому таблица
# chest_claims остаётся основным путём (миграция 20261102 / 20261106).
_PENDING_CHEST_CLAIMS: dict[str, dict] = {}
PENDING_CHEST_TTL = 12 * 3600


def _pending_claim_gc() -> None:
    deadline = time.time() - PENDING_CHEST_TTL
    for key in [k for k, v in _PENDING_CHEST_CLAIMS.items() if float(v.get("at", 0)) < deadline]:
        _PENDING_CHEST_CLAIMS.pop(key, None)


def _pending_claim_put(user_id, reward_coins: int, reward_keys: int,
                       reward_item: str = "", reward_item_amount: int = 0) -> str:
    """Кладёт награду «до кнопки Забрать» в память. Возвращает строковый claim_id."""
    _pending_claim_gc()
    claim_id = "mem-" + uuid.uuid4().hex[:12]
    _PENDING_CHEST_CLAIMS[claim_id] = {
        "user_id": str(user_id),
        "coins": int(reward_coins),
        "keys": int(reward_keys),
        # Капсула из сундука едет вместе с коинами: начисляется тоже по «Забрать».
        "item": str(reward_item or ""),
        "item_amount": int(reward_item_amount or 0),
        "at": time.time(),
    }
    return claim_id


def _pending_claim_take(claim_id, user_id) -> dict | None:
    """Забирает награду из памяти ровно один раз. None — нет/чужая."""
    entry = _PENDING_CHEST_CLAIMS.get(str(claim_id))
    if not entry or entry.get("user_id") != str(user_id):
        return None
    return _PENDING_CHEST_CLAIMS.pop(str(claim_id), None)


def _pending_claim_release(claim_id, entry: dict | None) -> None:
    """Возвращает награду на место, если запись баланса не удалась."""
    if not entry:
        return
    _PENDING_CHEST_CLAIMS[str(claim_id)] = entry


def _is_pending_claim(claim_id) -> bool:
    return isinstance(claim_id, str) and claim_id.startswith("mem-")


# ==================== КАПСУЛА ИЗ СУНДУКА ====================
# Шанс выпадения обычной капсулы: золотой сундук — 10%, обычный — 0.1%.
# Бросок независимый: капсула идёт В ДОБАВОК к коинам/ключу.
def chest_capsule_chance(chest_type: str) -> float:
    return CHEST_CAPSULE_CHANCE_GOLD if str(chest_type).lower() == "gold" else CHEST_CAPSULE_CHANCE_NORMAL


def roll_chest_capsule(chest_type: str) -> tuple[str, int]:
    """('capsule', 1) если капсула выпала, иначе ('', 0)."""
    chance = chest_capsule_chance(chest_type)
    if chance <= 0:
        return "", 0
    if roll_unit() < chance:
        return CHEST_CAPSULE_ITEM, CHEST_CAPSULE_AMOUNT
    return "", 0


# Капсула, привязанная к номеру выдачи. Нужна, только пока в базе нет колонок
# reward_item/reward_item_amount: награда не теряется и ждёт «Забрать» в памяти.
_CHEST_ITEM_BONUS: dict[str, dict] = {}
CHEST_ITEM_BONUS_LIMIT = 2000


def _chest_item_bonus_gc() -> None:
    deadline = time.time() - PENDING_CHEST_TTL
    for key in [k for k, v in _CHEST_ITEM_BONUS.items() if float(v.get("at", 0)) < deadline]:
        _CHEST_ITEM_BONUS.pop(key, None)
    # Жёсткий предел: словарь не должен расти бесконечно, даже если игроки
    # открывают сундуки и не забирают награду.
    while len(_CHEST_ITEM_BONUS) > CHEST_ITEM_BONUS_LIMIT:
        _CHEST_ITEM_BONUS.pop(next(iter(_CHEST_ITEM_BONUS)), None)


def _chest_item_bonus_put(claim_id, item_id: str, amount: int) -> None:
    if not item_id or int(amount or 0) <= 0:
        _CHEST_ITEM_BONUS.pop(str(claim_id), None)
        return
    _chest_item_bonus_gc()
    _CHEST_ITEM_BONUS[str(claim_id)] = {"item_id": item_id, "amount": int(amount), "at": time.time()}


def _chest_item_bonus_get(claim_id) -> tuple[str, int]:
    entry = _CHEST_ITEM_BONUS.get(str(claim_id))
    if not entry:
        return "", 0
    return str(entry.get("item_id") or ""), int(entry.get("amount") or 0)


def _chest_item_bonus_clear(claim_id) -> None:
    _CHEST_ITEM_BONUS.pop(str(claim_id), None)


def _chest_reward_item(row, claim_id=None) -> tuple[str, int]:
    """Что за предмет в этой выдаче: сначала колонки базы, потом память процесса."""
    item_id = str((row or {}).get(CHEST_ITEM_COLUMN) or "")
    amount = int((row or {}).get(CHEST_ITEM_AMOUNT_COLUMN) or 0)
    if item_id and amount > 0:
        return item_id, amount
    if claim_id is not None:
        return _chest_item_bonus_get(claim_id)
    return "", 0


def _claim_id_from_rows(rows):
    """id выдачи из ответа RPC (PostgREST отдаёт и список, и строку)."""
    if isinstance(rows, list) and rows:
        return (rows[0] or {}).get("claim_id")
    if isinstance(rows, dict):
        return rows.get("claim_id")
    return None


def _insert_chest_claim(user_id, chest_type: str, reward_coins: int, reward_keys: int,
                        reward_item: str = "", reward_item_amount: int = 0) -> dict:
    """Запоминаем разыгранную награду. Начисление — только в /api/claim_chest.

    Сначала пробуем SQL-функцию chest_claim_create (миграция
    20261102000000_stock_and_chest_rpc.sql, капсула — 20261108…): она пишет в
    таблицу от имени владельца, поэтому работает и при включённом RLS. Если
    функции нет — обычный insert. Любая ошибка → ChestUnavailable (начислим сразу).
    """
    reward_item = str(reward_item or "")
    reward_item_amount = int(reward_item_amount or 0) if reward_item else 0

    def _register(claim_id):
        # Память — страховка на случай базы без колонок reward_item*: капсула
        # не потеряется и придёт игроку по кнопке «Забрать».
        if reward_item:
            _chest_item_bonus_put(claim_id, reward_item, reward_item_amount)
        return {"id": claim_id, "reward_item": reward_item,
                "reward_item_amount": reward_item_amount}

    rpc_args = {
        "p_user_id": str(user_id),
        "p_chest_type": chest_type,
        "p_coins": int(reward_coins),
        "p_keys": int(reward_keys),
    }
    if reward_item:
        rpc_args["p_item"] = reward_item
        rpc_args["p_item_amount"] = reward_item_amount
    try:
        rows = supabase.rpc("chest_claim_create", rpc_args).execute().data
        claim_id = _claim_id_from_rows(rows)
        if claim_id is not None:
            return _register(claim_id)
        raise RuntimeError("chest_claim_create вернула пустой id")
    except Exception as exc:  # noqa: BLE001
        if not _rpc_missing(exc):
            _log_chest_error("chest_claim_create", exc, user_id)
            raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc
        logger.warning("RPC chest_claim_create недоступна (%s) — пишем в таблицу", exc)

    if reward_item:
        # Старая версия функции (без p_item/p_item_amount): награду не теряем —
        # пишем строку без капсулы, а капсулу держим в памяти до «Забрать».
        try:
            rows = supabase.rpc("chest_claim_create", {
                "p_user_id": str(user_id),
                "p_chest_type": chest_type,
                "p_coins": int(reward_coins),
                "p_keys": int(reward_keys),
            }).execute().data
            claim_id = _claim_id_from_rows(rows)
            if claim_id is not None:
                logger.warning(
                    "Сундук: капсула %s ждёт в памяти процесса — примените миграцию "
                    "supabase/migrations/20261108000000_chest_capsule_reward.sql", reward_item)
                return _register(claim_id)
        except Exception as exc:  # noqa: BLE001
            if not _rpc_missing(exc):
                _log_chest_error("chest_claim_create (без капсулы)", exc, user_id)
                raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc
            logger.warning("RPC chest_claim_create недоступна (%s) — пишем в таблицу", exc)

    payload = {
        "user_id": str(user_id),
        "chest_type": chest_type,
        "reward_coins": int(reward_coins),
        "reward_keys": int(reward_keys),
        # Явно: не полагаемся на DEFAULT в базе, чтобы фильтр .eq("claimed", False)
        # всегда находил строку.
        "claimed": False,
    }
    if reward_item:
        payload[CHEST_ITEM_COLUMN] = reward_item
        payload[CHEST_ITEM_AMOUNT_COLUMN] = reward_item_amount
    try:
        response = supabase.table(CHEST_CLAIMS_TABLE).insert(payload).execute()
    except Exception as exc:  # noqa: BLE001
        if not reward_item:
            _log_chest_error("insert chest_claims", exc, user_id)
            raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc
        # В таблице нет колонок reward_item* — пишем без них, капсула остаётся
        # в памяти процесса до кнопки «Забрать».
        logger.warning("chest_claims без колонок reward_item (%s) — капсула ждёт в памяти", exc)
        try:
            response = (
                supabase.table(CHEST_CLAIMS_TABLE)
                .insert({
                    "user_id": str(user_id),
                    "chest_type": chest_type,
                    "reward_coins": int(reward_coins),
                    "reward_keys": int(reward_keys),
                    "claimed": False,
                })
                .execute()
            )
        except Exception as exc2:  # noqa: BLE001
            _log_chest_error("insert chest_claims", exc2, user_id)
            raise ChestUnavailable(f"{type(exc2).__name__}: {exc2}") from exc2

    row = (response.data or [None])[0]
    if not row or row.get("id") is None:
        _log_chest_error("insert chest_claims: пустая строка", RuntimeError("пустой ответ"), user_id)
        raise ChestUnavailable("chest_claims insert вернул пустую строку")
    return _register(row.get("id"))


def _fetch_unclaimed_claims(user_id):
    """Незабранные награды игрока. ChestUnavailable — таблицы/функции нет."""
    try:
        rows = supabase.rpc("chest_claim_pending", {"p_user_id": str(user_id)}).execute().data
        return list(rows or [])
    except Exception as exc:  # noqa: BLE001
        if not _rpc_missing(exc):
            _log_chest_error("chest_claim_pending", exc, user_id)
            raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc
        logger.warning("RPC chest_claim_pending недоступна (%s) — читаем таблицу", exc)

    try:
        # SELECT * — чтобы получить и колонки капсулы (reward_item*), если миграция
        # применена. Колонки не выбираем по именам: на старой базе такой запрос
        # упал бы с PGRST204, и сундук перестал бы открываться вовсе.
        response = (
            supabase.table(CHEST_CLAIMS_TABLE)
            .select("*")
            .eq("user_id", str(user_id))
            .eq("claimed", False)
            .order("id")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        _log_chest_error("select chest_claims", exc, user_id)
        raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc
    return response.data or []


def _chest_take(claim_id, user_id) -> dict:
    """Атомарно «занимает» награду.

    {'found', 'already_claimed', 'reward_coins', 'reward_keys',
     'reward_item', 'reward_item_amount'}
    """
    try:
        rows = supabase.rpc("chest_claim_take", {
            "p_claim_id": int(claim_id), "p_user_id": str(user_id),
        }).execute().data
        if not rows:
            return {"found": False, "already_claimed": False, "reward_coins": 0,
                    "reward_keys": 0, "reward_item": "", "reward_item_amount": 0}
        row = rows[0] or {}
        item_id, item_amount = _chest_reward_item(row, claim_id)
        return {
            "found": True,
            "already_claimed": bool(row.get("already_claimed")),
            "reward_coins": int(row.get("reward_coins") or 0),
            "reward_keys": int(row.get("reward_keys") or 0),
            "reward_item": item_id,
            "reward_item_amount": item_amount,
        }
    except Exception as exc:  # noqa: BLE001
        if not _rpc_missing(exc):
            _log_chest_error("chest_claim_take", exc, user_id)
            raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc
        logger.warning("RPC chest_claim_take недоступна (%s) — работаем с таблицей", exc)

    try:
        response = (
            supabase.table(CHEST_CLAIMS_TABLE)
            .select("*")
            .eq("id", int(claim_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )
        row = (response.data or [None])[0]
        if not row:
            return {"found": False, "already_claimed": False, "reward_coins": 0,
                    "reward_keys": 0, "reward_item": "", "reward_item_amount": 0}
        if row.get("claimed"):
            return {"found": True, "already_claimed": True, "reward_coins": 0,
                    "reward_keys": 0, "reward_item": "", "reward_item_amount": 0}
        updated = (
            supabase.table(CHEST_CLAIMS_TABLE)
            .update({"claimed": True, "claimed_at": _now_iso()})
            .eq("id", int(claim_id))
            .eq("user_id", str(user_id))
            .eq("claimed", False)
            .select("id")
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        _log_chest_error("claim chest_claim id=%s" % claim_id, exc, user_id)
        raise ChestUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if not updated.data:
        return {"found": True, "already_claimed": True, "reward_coins": 0,
                "reward_keys": 0, "reward_item": "", "reward_item_amount": 0}
    item_id, item_amount = _chest_reward_item(row, claim_id)
    return {
        "found": True,
        "already_claimed": False,
        "reward_coins": int(row.get("reward_coins") or 0),
        "reward_keys": int(row.get("reward_keys") or 0),
        "reward_item": item_id,
        "reward_item_amount": item_amount,
    }


def _chest_release(claim_id, user_id) -> None:
    """Откат «занятия», если награду не удалось записать в профиль."""
    try:
        supabase.rpc("chest_claim_release", {
            "p_claim_id": int(claim_id), "p_user_id": str(user_id),
        }).execute()
        return
    except Exception as exc:  # noqa: BLE001
        if not _rpc_missing(exc):
            _log_chest_error("chest_claim_release", exc, user_id)
            return
    try:
        supabase.table(CHEST_CLAIMS_TABLE).update({"claimed": False, "claimed_at": None}).eq(
            "id", int(claim_id)).eq("user_id", str(user_id)).eq("claimed", True).execute()
    except Exception as exc:  # noqa: BLE001
        _log_chest_error("release chest_claim id=%s" % claim_id, exc, user_id)


@app.post("/api/open_chest")
async def open_chest(auth: OpenChestRequest):
    """Сундук только разыгрывает награду: коины и ключи придут после «Забрать».

    Так баланс не меняется до конца анимации рулетки (это был баг: начисление
    происходило в момент нажатия на сундук).
    """
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)  # техработы: во время них играют только админы
    user_id = str(user["id"])
    check_ban(user_id)

    chest_type = (auth.chest_type or "normal").strip().lower()

    # Идемпотентность: повторный запрос с тем же ключом вернёт уже открытый
    # сундук, а не потратит второй ключ/кулдаун.
    idem_key = _idempotency_key(user_id, auth.request_id, f"open_chest:{chest_type}")
    cached = idempotency_lookup(idem_key)
    if cached is not None:
        cached["idempotent_replay"] = True
        return cached
    if chest_type not in ("normal", "gold"):
        raise HTTPException(status_code=400, detail="Неизвестный тип сундука")

    now = int(time.time())
    try:
        row = _db_user(user_id)
    except Exception as exc:  # noqa: BLE001 — иначе игрок увидит голый 500
        logger.exception("Сундук: не удалось прочитать профиль user_id=%s", user_id)
        raise HTTPException(status_code=503, detail=_db_error_detail(
            exc, "Не удалось открыть сундук, попробуйте ещё раз")) from exc
    if row is None: raise HTTPException(status_code=404, detail="User not found")

    coins = int(row.get("coins", 0) or 0)
    keys = int(row.get("keys", 0) or 0)
    last_chest = int(row.get("last_chest", 0) or 0)

    # Незабранные награды прошлых сундуков не теряем: зачисляем их перед новой игрой
    # (закрытие старого сундука = согласие игрока, награда уже показана в рулетке).
    # Если chest_claims недоступна — сундук всё равно открываем, а награда этого
    # раунда ждёт кнопки «Забрать» в памяти процесса (_pending_claim_put).
    try:
        pending = await asyncio.to_thread(_fetch_unclaimed_claims, user_id)
    except ChestUnavailable as exc:
        _log_chest_error("незабранные награды недоступны", exc, user_id)
        pending = []
    except Exception as exc:  # noqa: BLE001
        _log_chest_error("чтение незабранных наград", exc, user_id)
        pending = []
    if pending:
        # Сначала «занимаем» старые награды условным апдейтом, потом зачисляем:
        # если зачисление не удалось — снимаем отметку, чтобы награда не пропала.
        locked_claims, taken_items = [], []
        for item in pending:
            try:
                taken = await asyncio.to_thread(_chest_take, item.get("id"), user_id)
                if taken.get("found") and not taken.get("already_claimed"):
                    locked_claims.append(item)
                    taken_items.append(taken)
            except Exception as exc:  # noqa: BLE001
                _log_chest_error("закрытие старого сундука id=%s" % item.get("id"), exc, user_id)
        extra_coins = sum(int(item.get("reward_coins") or 0) for item in locked_claims)
        extra_keys = sum(int(item.get("reward_keys") or 0) for item in locked_claims)
        # Капсулы из незабранных сундуков тоже не теряются.
        extra_items = []
        for taken in taken_items:
            item_id = str(taken.get("reward_item") or "")
            amount = int(taken.get("reward_item_amount") or 0)
            if item_id and amount > 0:
                extra_items.extend([item_id] * amount)
        if locked_claims:
            update_fields = {"coins": coins + extra_coins, "keys": keys + extra_keys}
            if extra_items:
                inventory = parse_inventory(row.get("inventory"))
                inventory.extend(extra_items)
                update_fields["inventory"] = json.dumps(inventory, ensure_ascii=False)
            if not _db_update_user(user_id, **update_fields):
                for item in locked_claims:
                    await asyncio.to_thread(_chest_release, item.get("id"), user_id)
                raise HTTPException(status_code=500, detail="Не удалось открыть сундук")
            for item in locked_claims:
                _chest_item_bonus_clear(item.get("id"))
        coins += extra_coins
        keys += extra_keys

    if chest_type == "gold":
        if keys <= 0:
            raise HTTPException(status_code=400, detail="У вас нет ключей!")
        keys -= 1
        reward_coins = (0 if roll_unit() < GOLD_KEY_BACK_CHANCE
                        else secure_int(GOLD_MIN_COINS, GOLD_MAX_COINS))
        reward_keys = 1 if reward_coins == 0 else 0
    else:
        if (now - last_chest) < CHEST_COOLDOWN:
            raise HTTPException(status_code=400, detail="Сундук перезаряжается")
        last_chest = now
        if roll_unit() < CHEST_KEY_CHANCE:
            reward_coins, reward_keys = 0, 1
        else:
            reward_coins, reward_keys = secure_int(CHEST_MIN_COINS, CHEST_MAX_COINS), 0

    # Обычная капсула: отдельный бросок в ДОБАВОК к коинам/ключу.
    # Золотой сундук — 10%, обычный — 0.1%.
    reward_item, reward_item_amount = roll_chest_capsule(chest_type)

    claim = None
    try:
        claim = await asyncio.to_thread(_insert_chest_claim, user_id, chest_type, reward_coins,
                                        reward_keys, reward_item, reward_item_amount)
    except ChestUnavailable as exc:
        # chest_claims недоступна — НЕ начисляем награду сейчас (баланс должен
        # меняться только по «Забрать»), а держим её в памяти процесса.
        _log_chest_error("chest_claims недоступна → награда ждёт «Забрать» в памяти", exc, user_id)
        claim = {"id": _pending_claim_put(user_id, reward_coins, reward_keys,
                                          reward_item, reward_item_amount)}
        print(f"СУНДУК: награда отложена в памяти user_id={user_id} claim={claim['id']} "
              f"(+{reward_coins} коинов, +{reward_keys} ключей, "
              f"капсула={reward_item or 'нет'} x{reward_item_amount})", flush=True)
    except Exception as exc:  # noqa: BLE001
        _log_chest_error("создание chest_claim", exc, user_id)
        raise HTTPException(status_code=503, detail=_chest_claims_detail(exc)) from exc

    # Ключ тратится сразу (он оплачивает попытку), кулдаун тоже стартует сразу,
    # а сами коины/ключи из награды — только по кнопке «Забрать».
    if not _db_update_user(user_id, coins=coins, keys=keys, last_chest=last_chest):
        raise HTTPException(status_code=500, detail="Не удалось открыть сундук")

    payload = {
        "success": True, "chest_type": chest_type,
        "reward_coins": reward_coins, "reward_keys": reward_keys,
        # Капсула (если выпала) — отдельным полем: клиент рисует её в рулетке и
        # в окне награды. Начисляется тоже по кнопке «Забрать».
        "reward_item": reward_item, "reward_item_amount": reward_item_amount,
        "capsule_chance": chest_capsule_chance(chest_type),
        "claim_id": claim.get("id"),
        "credited": False, "deferred": True,
        "new_balance": coins, "new_keys": keys, "last_chest": last_chest,
    }
    idempotency_store(idem_key, user_id, f"open_chest:{chest_type}", payload)
    return payload


@app.post("/api/claim_chest")
async def claim_chest(auth: "ClaimChestRequest"):
    """Начисляет награду сундука. Идемпотентно: повторный запрос не платит дважды."""
    user = verify_telegram_data(auth.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)  # техработы: во время них играют только админы
    user_id = int(user["id"])
    check_ban(str(user_id))

    if auth.claim_id is None:
        raise HTTPException(status_code=400, detail="Не указан сундук")

    # Профиль читаем ДО «занятия» награды: если строки пользователя нет, выдача
    # останется незабранной и не потеряется.
    try:
        row = _db_user(str(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Сундук: не удалось прочитать профиль user_id=%s", user_id)
        raise HTTPException(status_code=503, detail=_db_error_detail(
            exc, "Не удалось забрать награду, попробуйте ещё раз")) from exc
    if row is None: raise HTTPException(status_code=404, detail="User not found")
    coins = int(row.get("coins", 0) or 0)
    keys = int(row.get("keys", 0) or 0)

    if _is_pending_claim(auth.claim_id):
        # Награда из памяти: «занимаем» её один раз, при сбое вернём обратно.
        pending = _pending_claim_take(auth.claim_id, user_id)
        if pending is None:
            raise HTTPException(
                status_code=404,
                detail="Награда не найдена (сервер перезапускался). Открой сундук ещё раз")
        reward_coins = int(pending.get("coins") or 0)
        reward_keys = int(pending.get("keys") or 0)
        reward_item = str(pending.get("item") or "")
        reward_item_amount = int(pending.get("item_amount") or 0) if reward_item else 0
        new_coins = coins + reward_coins
        new_keys = keys + reward_keys
        update_fields = {"coins": new_coins, "keys": new_keys}
        inventory = None
        if reward_item and reward_item_amount > 0:
            inventory = parse_inventory(row.get("inventory"))
            inventory.extend([reward_item] * reward_item_amount)
            update_fields["inventory"] = json.dumps(inventory, ensure_ascii=False)
        if not _db_update_user(str(user_id), **update_fields):
            _pending_claim_release(auth.claim_id, pending)
            raise HTTPException(status_code=500, detail="Не удалось забрать награду")
        _chest_item_bonus_clear(auth.claim_id)
        print(f"СУНДУК: награда забрана из памяти user_id={user_id} "
              f"+{reward_coins} коинов, +{reward_keys} ключей, "
              f"капсула={reward_item or 'нет'} x{reward_item_amount}", flush=True)
        return {"success": True, "already_claimed": False, "reward_coins": reward_coins,
                "reward_keys": reward_keys,
                "reward_item": reward_item, "reward_item_amount": reward_item_amount,
                "inventory": inventory, "new_balance": new_coins, "new_keys": new_keys}

    try:
        claim = await asyncio.to_thread(_chest_take, auth.claim_id, user_id)
    except ChestUnavailable as exc:
        _log_chest_error("chest_claim_take id=%s" % auth.claim_id, exc, user_id)
        raise HTTPException(status_code=503, detail=_chest_claims_detail(exc)) from exc
    if not claim.get("found"):
        raise HTTPException(status_code=404, detail="Сундук не найден")

    if claim.get("already_claimed"):
        return {"success": True, "already_claimed": True, "reward_coins": 0, "reward_keys": 0,
                "reward_item": "", "reward_item_amount": 0,
                "new_balance": coins, "new_keys": keys}

    reward_coins = int(claim.get("reward_coins") or 0)
    reward_keys = int(claim.get("reward_keys") or 0)
    reward_item = str(claim.get("reward_item") or "")
    reward_item_amount = int(claim.get("reward_item_amount") or 0) if reward_item else 0
    new_coins = coins + reward_coins
    new_keys = keys + reward_keys
    update_fields = {"coins": new_coins, "keys": new_keys}
    inventory = None
    if reward_item and reward_item_amount > 0:
        # Капсула из сундука попадает в инвентарь — тоже только по «Забрать».
        inventory = parse_inventory(row.get("inventory"))
        inventory.extend([reward_item] * reward_item_amount)
        update_fields["inventory"] = json.dumps(inventory, ensure_ascii=False)
    if not _db_update_user(str(user_id), **update_fields):
        # Награду не записали — снимаем отметку, чтобы игрок мог нажать «Забрать» ещё раз.
        await asyncio.to_thread(_chest_release, auth.claim_id, user_id)
        raise HTTPException(status_code=500, detail="Не удалось забрать награду")
    _chest_item_bonus_clear(auth.claim_id)

    print(f"СУНДУК: награда забрана user_id={user_id} +{reward_coins} коинов, "
          f"+{reward_keys} ключей, капсула={reward_item or 'нет'} x{reward_item_amount}",
          flush=True)
    return {"success": True, "already_claimed": False, "reward_coins": reward_coins,
            "reward_keys": reward_keys,
            "reward_item": reward_item, "reward_item_amount": reward_item_amount,
            "inventory": inventory, "new_balance": new_coins, "new_keys": new_keys}


@app.post("/api/shop_items")
async def get_shop_items(data: ShopCatalogRequest):
    """Shop catalog for the current user. Admin-only capsules are never included for regular users,
    а распроданные по глобальному тиражу не показываются никому."""
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = int(user["id"])
    check_ban(str(user_id))
    is_admin = is_admin_id(user_id, ADMIN_IDS)
    # Лимиты отключены — в базу за тиражом не ходим вовсе.
    stock = await asyncio.to_thread(shop_stock_snapshot, supabase) if STOCK_LIMITS_ENABLED else None
    return {
        "is_admin": is_admin,
        "items": serialize_shop_items(is_admin, stock),
        # Распроданные предметы: витрина должна сказать «Всё распродано»,
        # а не «Магазин пуст» — иначе это выглядит как поломка.
        "sold_out": sold_out_items(is_admin, stock),
        "stock_ready": (stock is not None) if STOCK_LIMITS_ENABLED else False,
        "unlimited": not STOCK_LIMITS_ENABLED,
    }


@app.get("/api/stock_debug")
async def stock_debug():
    """Диагностика тиража капсул: доходит ли покупка до RPC и что отвечает база.

    Ничего не списывает (пробный вызов с несуществующим item_id). Открывается
    в браузере: https://<сервис>/api/stock_debug — сразу видно, применена ли
    SQL-функция, какая у неё сигнатура и какой остаток в таблице.
    """
    try:
        report = await asyncio.to_thread(stock_diagnostics, supabase)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=200, content={
            "ok": False, "build": API_BUILD,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        })
    report["missing_rows"] = missing_stock_rows(report.get("snapshot"))
    report["ok"] = bool(report.get("rpc_ready"))
    report["build"] = API_BUILD
    report["how_to_read"] = {
        "rpc_ready": "true — функция shop_take_stock найдена и отвечает",
        "attempts[].verdict": "sold_out на пробном id — правильный ответ (нечего списывать)",
        "snapshot": "остаток, который сейчас видит магазин",
    }
    return report


@app.post("/api/buy_item")
async def buy_item(data: BuyItem):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)  # техработы: во время них играют только админы
    user_id = int(user["id"])
    check_ban(str(user_id))
    if data.item_id not in SHOP_ITEMS:
        raise HTTPException(status_code=400, detail="Неизвестный предмет магазина")

    # Идемпотентность: двойной тап по «Купить» не должен списать коины дважды.
    idem_key = _idempotency_key(user_id, data.request_id, "buy_item")
    cached = idempotency_lookup(idem_key)
    if cached is not None:
        cached["idempotent_replay"] = True
        return cached

    try:
        result = await asyncio.to_thread(purchase_shop_item, supabase, user_id, data.item_id,
                                         ADMIN_IDS, "mini")
    except ShopPurchaseError as exc:
        print(f"МАГАЗИН: отказ user_id={user_id} item={data.item_id} "
              f"[{exc.status_code}] {exc.message}", flush=True)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:  # noqa: BLE001 — любая поломка базы должна быть читаемой
        logger.exception("МАГАЗИН: непредвиденная ошибка покупки user_id=%s item=%s",
                         user_id, data.item_id)
        raise HTTPException(status_code=503, detail=_db_error_detail(
            exc, "Не удалось завершить покупку, попробуйте ещё раз")) from exc
    if STOCK_LIMITS_ENABLED and not result.get("stock_ready", True):
        # Купили, но тираж не списан: говорим прямо, а не делаем вид, что всё хорошо.
        print(f"МАГАЗИН: ВНИМАНИЕ тираж не списан (stock_ready=false) item={data.item_id} — "
              f"примените supabase/migrations/20261104000000_shop_stock_fix.sql", flush=True)
        result["stock_warning"] = "Остаток тиража не обновился: база не дала списать"
    # Одна строка на покупку: по ней видно и баланс, и остаток тиража.
    print(f"МАГАЗИН: куплено user_id={user_id} item={data.item_id} "
          f"баланс→{result.get('new_balance')} остаток→{result.get('stock')} "
          f"stock_ready={result.get('stock_ready')}", flush=True)
    if isinstance(result, dict):
        idempotency_store(idem_key, user_id, "buy_item", result)
    return result

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


def upgrade_multiplier_chance(multiplier) -> float:
    """Шанс для множителя X: 90 / X, обрезанный в допустимые рамки.

    Одна и та же формула у кнопок и у «своего» X: игрок выбирает x3.7 —
    получает 24.32%, выбирает x1.75 — 51.43%. Никакой таблицы-лотереи.
    """
    try:
        value = float(multiplier)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    chance = UPGRADE_CHANCE_NUMERATOR / value
    return round(max(UPGRADE_MIN_CHANCE, min(UPGRADE_MAX_CHANCE, chance)), 2)


def _upgrade_target_value(source: str, amount: int, target: str,
                          multiplier: Optional[float], chance: Optional[float]) -> tuple[int, float]:
    """Возвращает (стоимость цели, визуальный шанс успеха в процентах)."""
    from_value = _upgrade_source_value(source, amount)

    priced_chance = None
    if target == "coin":
        if chance is not None:
            if chance <= 0 or chance > UPGRADE_MAX_CHANCE:
                raise HTTPException(status_code=400, detail=f"Шанс должен быть от 0 до {int(UPGRADE_MAX_CHANCE)}%")
            to_value = int(round(from_value / (chance / 100.0)))
            priced_chance = float(chance)
        else:
            try:
                multiplier = float(multiplier or 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Некорректный коэффициент")
            # Диапазон вместо списка: с задачи 27 игрок сам выбирает X ползунком
            # (1.12 … 50). Список оставлен только для быстрых кнопок.
            if multiplier < UPGRADE_MIN_MULTIPLIER - 1e-9:
                raise HTTPException(status_code=400,
                                    detail=f"Минимальный множитель x{UPGRADE_MIN_MULTIPLIER}")
            if multiplier > UPGRADE_MAX_MULTIPLIER + 1e-9:
                raise HTTPException(status_code=400,
                                    detail=f"Максимальный множитель x{int(UPGRADE_MAX_MULTIPLIER)}")
            to_value = int(round(from_value * multiplier))
            # Шанс по одной формуле 90 / X — что нарисовано, то и играется.
            priced_chance = upgrade_multiplier_chance(multiplier)
        win_amount = to_value
    elif target in UPGRADE_ITEM_PRICES:
        # Цель — капсула: за успех выдаём ровно одну штуку.
        to_value = UPGRADE_ITEM_PRICES[target]
        win_amount = 1

    if to_value <= 0:
        raise HTTPException(status_code=400, detail="Некорректная цель апгрейда")
    if to_value > UPGRADE_MAX_REWARD:
        # Сработает только на заведомо мусорных числах из запроса: штатные
        # комбинации (до 10 000 коинов × x50) проходят с запасом.
        raise HTTPException(status_code=400, detail="Слишком крупная цель — выбери что-то поменьше")

    visual_chance = (priced_chance if priced_chance is not None
                     else from_value / to_value * 100.0)
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
# Каждый розыгрыш пишется в upgrade_log: что ставили, какой был шанс, какой был бросок,
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
    if UPGRADE_ADMIN_ONLY and not admin:
        # Техработы закончились — играют все, НО апгрейд пока только админам.
        raise HTTPException(status_code=403, detail=UPGRADE_ADMIN_ONLY_MESSAGE)

    check_ban(str(user_id))

    # Идемпотентность: повторная отправка того же апгрейда не спишет ставку снова.
    idem_key = _idempotency_key(user_id, data.request_id, "upgrade")
    cached = idempotency_lookup(idem_key)
    if cached is not None:
        cached["idempotent_replay"] = True
        return cached

    source, amount, target, _ = _upgrade_normalize_request(data)

    if source == "coin":
        if amount < UPGRADE_MIN_BET:
            raise HTTPException(status_code=400, detail=f"Минимальная ставка {UPGRADE_MIN_BET} 🪙")
        if amount > UPGRADE_MAX_BET:
            raise HTTPException(status_code=400, detail=f"Максимальная ставка {UPGRADE_MAX_BET}")
    else:
        if amount < 1:
            raise HTTPException(status_code=400, detail="Минимум 1 капсула")
        if amount > UPGRADE_MAX_CAPSULES:
            raise HTTPException(status_code=400, detail=f"Максимум {UPGRADE_MAX_CAPSULES} капсул за раз")

    win_amount, visual_chance = _upgrade_target_value(
        source, amount, target, data.target_multiplier, data.target_chance
    )

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

        # ДОМ-ЭДЖ больше не зависит от баланса (он уже в таблице множителей).
        # Значение из game_settings применяется ко ВСЕМ и уходит клиенту в
        # /api/user, поэтому круг рисует ровно тот шанс, по которому играем.
        edge = game_setting_float("upgrade_house_edge", UPGRADE_HOUSE_EDGE_FACTOR)
        real_chance = visual_chance * edge
        # Админ может подкрутить шансы без деплоя (таблица game_settings).
        real_chance *= game_setting_float("upgrade_chance_factor", 1.0)
        if not admin:
            real_chance /= UPGRADE_NON_ADMIN_DIVIDER

        # ВАЖНО: и вердикт, и стрелка считаются по ОДНОМУ числу — по тому, которое
        # уходит клиенту в поле "real_chance". Клиент рисует зелёную зону 0..chance
        # и ставит стрелку на roll * 3.6°, поэтому любое расхождение в округлении
        # сразу даёт «стрелка в красной зоне, а засчитали победу».
        chance_verdict = round(min(100.0, max(0.0, float(real_chance))), 2)
        # Бросок: криптостойкий ГСЧ (secrets), до сотых — клиент ставит угол
        # roll * 3.6°. Mersenne Twister здесь недопустим: по нескольким выдачам
        # его состояние восстанавливается, и «честный» рандом становится
        # предсказуемым.
        roll = roll_percent()
        # Правило игрока: roll <= chance — победа, roll > chance — проигрыш.
        # Граница (roll == chance) принадлежит ЗЕЛЁНОЙ зоне, ровно как заливка
        # на клиенте: зелёная 0..chance, красная chance..100.
        won = roll <= chance_verdict
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
                chance=chance_verdict, roll=roll, won=won, win_item=win_item,
                win_amount=payout,
                coins_before=raw_coins, coins_after=new_coins, inventory_after=new_inventory,
            )
            payload = {
                "success": won,
                "won": won,
                "item_from": source,
                "amount_from": amount,
                "item_to": target,
                # Шанс, который играли: клиент рисует по нему зелёную зону и
                # подпись «шанс успеха», поэтому число всегда совпадает с вердиктом.
                "chance": chance_verdict,
                # Расчётный (рекламный) шанс без дом-эджа — только для диагностики.
                "advertised_chance": round(visual_chance, 2),
                "house_edge": edge,
                # Реальный шанс = то же число, что участвовало в сравнении.
                # Клиент рисует сектор и ставит стрелку именно по нему,
                # поэтому геометрия круга и вердикт не могут разойтись.
                "real_chance": chance_verdict,
                "roll": roll,
                "log_id": log_id,
                "win_item": win_item,
                "win_amount": payout,
                "new_balance": new_coins,
                "inventory": new_inventory,
                "new_inventory": new_inventory,
            }
            idempotency_store(idem_key, user_id, "upgrade", payload)
            return payload

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
    if UPGRADE_ADMIN_ONLY and not is_admin_id(user_id, ADMIN_IDS):
        raise HTTPException(status_code=403, detail=UPGRADE_ADMIN_ONLY_MESSAGE)

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
    if UPGRADE_ADMIN_ONLY and not is_admin_id(user_id, ADMIN_IDS):
        raise HTTPException(status_code=403, detail=UPGRADE_ADMIN_ONLY_MESSAGE)

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
    require_play_access(user)  # техработы: во время них играют только админы
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
    require_play_access(user)  # техработы: во время них играют только админы
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



# ==================== ПОДПИСКА / ПАССИВКА / РЕФЕРАЛЫ / STARS ====================
class ChannelCheckRequest(BaseModel):
    initData: str
    request_id: Optional[str] = None


class PassiveIncomeRequest(BaseModel):
    initData: str


class ReferralBindRequest(BaseModel):
    initData: str
    ref_code: Optional[str] = None


class StarsInvoiceRequest(BaseModel):
    initData: str
    package_id: Optional[str] = None


class AdminSettingRequest(BaseModel):
    initData: str
    key: str
    value: Optional[str] = None


class AdminGiveRequest(BaseModel):
    initData: str
    user_id: int
    amount: int
    request_id: Optional[str] = None


class AdminBanRequest(BaseModel):
    initData: str
    user_id: int
    banned: bool = True


def _auth_user_id(init_data: str) -> int:
    """Единая расшифровка initData → user_id (401/400 вместо трейсбека)."""
    user = verify_telegram_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    uid = _safe_int(user.get("id"), None)
    if uid is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    check_ban(str(uid))
    return uid


def _require_admin_api(init_data: str) -> int:
    uid = _auth_user_id(init_data)
    if not is_admin_id(uid, ADMIN_IDS):
        raise HTTPException(status_code=403, detail="Только для администраторов")
    return uid


def award_coins(user_id, amount) -> Optional[int]:
    """Начисление коинов на сервере: читаем актуальный баланс, пишем новый.

    Возвращает новый баланс или None, если запись не прошла. Все начисления
    (подписка, пассивка, рефералы, Stars) идут только через эту функцию —
    клиент не может прибавить себе коины запросом.
    """
    uid = _safe_int(user_id, None)
    if uid is None:
        return None
    row = _db_user(uid)
    if row is None:
        return None
    coins = _safe_int(row.get("coins"), 0) + int(amount)
    if coins < 0:
        coins = 0
    return coins if _db_update_user(uid, coins=coins) else None


def friendly_telegram_error(description: str) -> str:
    """Сырой ответ Telegram → текст, который не стыдно показать игроку.

    Самый частый случай — «Bad Request: member list is inaccessible»: бот не
    администратор канала, и Telegram отказывает в getChatMember. Раньше это
    улетало в Mini App как есть (страшный алерт после перехода в канал).
    """
    text = str(description or "").strip()
    low = text.lower()
    if not text:
        return "Не удалось проверить подписку. Попробуйте позже."
    if "member list is inaccessible" in low or "not enough rights" in low:
        return (f"Не удалось проверить подписку: бот должен быть администратором "
                f"канала @{CHANNEL_USERNAME}.")
    if "chat not found" in low or "chat_id is empty" in low:
        return f"Канал @{CHANNEL_USERNAME} не найден. Проверьте CHANNEL_USERNAME на сервере."
    if "user not found" in low or "participant_id_invalid" in low or "user_id_invalid" in low:
        return "Тебя нет в канале — подпишись и нажми «Проверить» ещё раз."
    if "bot was blocked by the user" in low or "bot is not a member" in low:
        return f"Бот не может проверить канал @{CHANNEL_USERNAME}: добавьте его администратором."
    if "too many requests" in low or "retry after" in low:
        return "Слишком много проверок подряд. Подождите минуту и попробуйте снова."
    # Всё остальное — без «Bad Request:» и кода ошибки.
    cleaned = text.split(":", 1)[1].strip() if low.startswith("bad request") else text
    return f"Не удалось проверить подписку: {cleaned[:160]}"


async def telegram_chat_member_status(user_id) -> dict:
    """Статус подписки на канал через getChatMember.

    Возвращает {"ok": bool, "status": str, "subscribed": bool, "detail": str}.
    Сетевые/токеновые ошибки не роняют эндпоинт: игрок увидит «не удалось
    проверить», а не 500.
    """
    if not BOT_TOKEN:
        return {"ok": False, "status": "", "subscribed": False,
                "detail": "BOT_TOKEN не задан на сервисе aura-api"}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    params = {"chat_id": f"@{CHANNEL_USERNAME}", "user_id": int(user_id)}
    try:
        payload = await _http_json(url, params=params, timeout=8)
    except Exception as exc:  # noqa: BLE001
        logger.warning("getChatMember недоступен: %s", exc)
        return {"ok": False, "status": "", "subscribed": False,
                "detail": "Не удалось проверить подписку, попробуйте позже"}
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = (payload or {}).get("description") if isinstance(payload, dict) else ""
        logger.warning("getChatMember отказ: %s", description)
        return {"ok": False, "status": "", "subscribed": False,
                "detail": friendly_telegram_error(description)}
    status = str((payload.get("result") or {}).get("status") or "")
    subscribed = status in ("creator", "administrator", "member")
    return {"ok": True, "status": status, "subscribed": subscribed,
            "detail": "" if subscribed else "Вы ещё не подписаны на канал"}


def _subscription_claimed(user_id) -> Optional[bool]:
    """True/False — было ли начисление. None — таблица недоступна."""
    try:
        res = (supabase.table(SUBSCRIPTIONS_TABLE).select("user_id, bonus_claimed")
               .eq("user_id", int(user_id)).limit(1).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("subscriptions_history недоступна: %s", exc)
        return None
    return bool(res.data)


@app.post("/api/channel_check")
async def channel_check(data: ChannelCheckRequest):
    """Проверка подписки + одноразовый бонус 200 коинов.

    Порядок важен для честности: сначала getChatMember, потом «одноразовая»
    отметка в базе, и только если её удалось поставить — начисление. Если
    отметка уже стоит, второй раз коины не придут, даже если игрок
    отписался и подписался снова.
    """
    uid = _auth_user_id(data.initData)

    # Сначала смотрим, забирал ли игрок бонус. Если да — Telegram вообще не
    # дёргаем: иначе у всех, кто уже получил 200 коинов, вылезал сырой
    # «Bad Request: member list is inaccessible» вместо человеческого ответа.
    claimed = _subscription_claimed(uid)
    if claimed is None:
        raise HTTPException(status_code=503, detail=(
            "Таблица subscriptions_history не создана. Примените миграцию "
            "supabase/migrations/20261109000000_economy_social.sql"))
    if claimed:
        return {"ok": True, "subscribed": True, "bonus": SUBSCRIPTION_BONUS,
                "bonus_claimed": True, "granted": 0, "channel_url": CHANNEL_URL,
                "detail": "Бонус за подписку уже получен — повторно он не начисляется."}

    member = await telegram_chat_member_status(uid)
    if not member["subscribed"]:
        return {
            "ok": bool(member["ok"]),
            "subscribed": False,
            "bonus": SUBSCRIPTION_BONUS,
            "bonus_claimed": False,
            "channel_url": CHANNEL_URL,
            "detail": member["detail"],
        }

    # Ставим отметку ДО начисления: user_id — первичный ключ, поэтому две
    # параллельные проверки не смогут выдать бонус дважды.
    try:
        supabase.table(SUBSCRIPTIONS_TABLE).insert({
            "user_id": uid, "bonus_claimed": True, "reward": SUBSCRIPTION_BONUS,
            "created_at": _now_iso(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        if _subscription_claimed(uid):
            return {"ok": True, "subscribed": True, "bonus": SUBSCRIPTION_BONUS,
                    "bonus_claimed": True, "granted": 0, "channel_url": CHANNEL_URL,
                    "detail": "Бонус за подписку уже получен"}
        raise HTTPException(status_code=503, detail=_db_error_detail(
            exc, "Не удалось зафиксировать бонус, попробуйте позже")) from exc

    coins = award_coins(uid, SUBSCRIPTION_BONUS)
    if coins is None:
        # Начисление не прошло — снимаем отметку, чтобы бонус не «сгорел».
        try:
            supabase.table(SUBSCRIPTIONS_TABLE).delete().eq("user_id", uid).execute()
        except Exception:
            pass
        raise HTTPException(status_code=503, detail="Не удалось начислить бонус, попробуйте ещё раз")

    return {"ok": True, "subscribed": True, "bonus": SUBSCRIPTION_BONUS,
            "bonus_claimed": True, "granted": SUBSCRIPTION_BONUS,
            "new_balance": coins, "channel_url": CHANNEL_URL,
            "detail": f"Бонус +{SUBSCRIPTION_BONUS} коинов за подписку"}


def _passive_rate_for(inventory) -> tuple[float, dict]:
    """Сколько коинов в час приносит инвентарь + разбивка по предметам."""
    counts: dict = {}
    for item in inventory or []:
        item_id = str(item)
        counts[item_id] = counts.get(item_id, 0) + 1
    rate = sum(PASSIVE_INCOME_RATES.get(item_id, 0.0) * count
               for item_id, count in counts.items())
    breakdown = {item_id: {"count": count, "rate": PASSIVE_INCOME_RATES.get(item_id, 0.0),
                           "per_hour": PASSIVE_INCOME_RATES.get(item_id, 0.0) * count}
                 for item_id, count in counts.items() if PASSIVE_INCOME_RATES.get(item_id)}
    return round(rate, 4), breakdown


def _passive_last_at(user_id):
    """Момент последнего начисления. None — ещё не начисляли, 'error' — нет таблицы."""
    try:
        res = (supabase.table(PASSIVE_INCOME_TABLE).select("last_at")
               .eq("user_id", int(user_id)).limit(1).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("passive_income недоступна: %s", exc)
        return "error"
    if not res.data:
        return None
    return _parse_timestamp(res.data[0].get("last_at"))


def _passive_touch(user_id, stamp: float) -> bool:
    try:
        supabase.table(PASSIVE_INCOME_TABLE).upsert(
            {"user_id": int(user_id), "last_at": _iso_from_timestamp(stamp)},
            on_conflict="user_id").execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось сохранить passive_income %s: %s", user_id, exc)
        return False


def _iso_from_timestamp(stamp: float) -> str:
    return datetime.datetime.fromtimestamp(float(stamp), datetime.timezone.utc).isoformat()


@app.post("/api/passive_income")
async def passive_income(data: PassiveIncomeRequest):
    """Пассивный доход с капсул: начисляется сервером по фактическому инвентарю.

    Формула: сумма(кол-во капсул * ставка в час) * прошедшие часы, но не больше
    24 часов за один заход. Начисление идемпотентно по времени: как только
    коины выданы, отметка last_at сдвигается ровно на «оплаченные» часы.
    """
    uid = _auth_user_id(data.initData)
    row = _db_user(uid)
    if row is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    inventory = parse_inventory(row.get("inventory"))
    rate, breakdown = _passive_rate_for(inventory)
    now = time.time()

    last = _passive_last_at(uid)
    if last == "error":
        raise HTTPException(status_code=503, detail=(
            "Таблица passive_income не создана. Примените миграцию "
            "supabase/migrations/20261109000000_economy_social.sql"))
    if last is None:
        _passive_touch(uid, now)
        return {"ok": True, "accrued": 0, "rate": rate, "hours": 0.0,
                "breakdown": breakdown, "new_balance": _safe_int(row.get("coins"), 0),
                "detail": "Пассивный доход запущен — заходите позже"}

    hours = max(0.0, min(PASSIVE_INCOME_MAX_HOURS, (now - float(last)) / 3600.0))
    entitled = rate * hours
    accrued = int(entitled)          # дробные коины копим, а не теряем
    coins_now = _safe_int(row.get("coins"), 0)
    if accrued < 1 or rate <= 0:
        return {"ok": True, "accrued": 0, "rate": rate, "hours": round(hours, 3),
                "entitled": round(entitled, 3), "breakdown": breakdown,
                "new_balance": coins_now,
                "detail": "Накопление идёт" if rate > 0 else "Нет капсул для пассивного дохода"}

    new_coins = award_coins(uid, accrued)
    if new_coins is None:
        raise HTTPException(status_code=503, detail="Не удалось начислить пассивный доход")
    # Сдвигаем отметку ровно на оплаченные часы: остаток «с копейками» остаётся.
    consumed_hours = accrued / rate
    _passive_touch(uid, min(now, float(last) + consumed_hours * 3600.0))
    return {"ok": True, "accrued": accrued, "rate": rate, "hours": round(hours, 3),
            "breakdown": breakdown, "new_balance": new_coins,
            "detail": f"+{accrued} коинов пассивного дохода"}


# ---------- Рефералы ----------
def _nft_count(inventory) -> int:
    """«NFT» этого проекта: лимитированные капсулы + всё, что начинается с nft."""
    total = 0
    for item in inventory or []:
        item_id = str(item).lower()
        if item_id in REFERRAL_NFT_ITEMS or item_id.startswith("nft"):
            total += 1
    return total


def _referral_reward_for(row) -> int:
    inventory = parse_inventory(row.get("inventory"))
    nft = _nft_count(inventory)
    premium = bool(row.get("premium"))
    if premium and nft >= 1:
        return REFERRAL_REWARD_PREMIUM
    if nft >= 2:
        return REFERRAL_REWARD_NFT2
    return REFERRAL_REWARD_BASE


def _referral_parse_code(raw) -> Optional[int]:
    """ref_123 / 123 / https://t.me/bot?startapp=ref_123 → 123."""
    text = str(raw or "").strip()
    if not text:
        return None
    if "startapp=" in text:
        text = text.split("startapp=", 1)[1]
    elif "start=" in text:
        text = text.split("start=", 1)[1]
    text = text.split("&", 1)[0]
    if text.lower().startswith("ref_"):
        text = text[4:]
    return _safe_int(text, None)


def _referral_rows_for_referrer(user_id, limit: int = 100):
    try:
        res = (supabase.table(REFERRALS_TABLE).select("*")
               .eq("referrer_id", int(user_id)).limit(limit).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("referrals недоступна: %s", exc)
        return "error"
    return res.data or []


def _referral_stats(user_id) -> dict:
    rows = _referral_rows_for_referrer(user_id)
    if rows == "error":
        return {"available": False, "count": 0, "earned": 0, "invited": [],
                "detail": "Примените миграцию 20261109000000_economy_social.sql"}
    earned = sum(_safe_int(row.get("reward"), 0) for row in rows)
    return {
        "available": True,
        "count": len(rows),
        "earned": earned,
        "invited": [{"user_id": row.get("referred_id"),
                     "reward": _safe_int(row.get("reward"), 0),
                     "status": row.get("status") or "pending"} for row in rows[:20]],
        "link": f"https://t.me/{{bot}}?startapp=ref_{int(user_id)}",
    }


@app.post("/api/referral_bind")
async def referral_bind(data: ReferralBindRequest):
    """Привязать пригласившего. Работает один раз: у referred_id уникальный ключ."""
    uid = _auth_user_id(data.initData)
    referrer_id = _referral_parse_code(data.ref_code)
    if referrer_id is None:
        return {"ok": False, "detail": "Некорректный код приглашения"}
    if referrer_id == uid:
        return {"ok": False, "detail": "Нельзя пригласить самого себя"}
    if _db_user(referrer_id) is None:
        return {"ok": False, "detail": "Пригласивший не найден"}
    try:
        res = (supabase.table(REFERRALS_TABLE).select("referred_id")
               .eq("referred_id", uid).limit(1).execute())
        if res.data:
            return {"ok": True, "already": True, "detail": "Приглашение уже учтено"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=(
            "Таблица referrals не создана. Примените миграцию "
            "supabase/migrations/20261109000000_economy_social.sql")) from exc
    try:
        supabase.table(REFERRALS_TABLE).insert({
            "referred_id": uid, "referrer_id": referrer_id, "reward": 0,
            "status": "pending", "created_at": _now_iso(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось записать реферала %s → %s: %s", referrer_id, uid, exc)
        return {"ok": False, "detail": "Не удалось сохранить приглашение"}
    return {"ok": True, "referrer_id": referrer_id, "status": "pending"}


@app.post("/api/referral_check")
async def referral_check(data: UserAuth):
    """Проверка статуса реферала при его входе в Mini App.

    Награда достаётся пригласившему и начисляется один раз: статус в таблице
    переводится из pending в paid, повторный вызов ничего не платит.
    """
    uid = _auth_user_id(data.initData)
    try:
        res = (supabase.table(REFERRALS_TABLE).select("*")
               .eq("referred_id", uid).limit(1).execute())
    except Exception:  # noqa: BLE001 — таблицы нет: это не ошибка входа
        return {"ok": True, "available": False, "paid": False}
    if not res.data:
        return {"ok": True, "available": True, "referred": False, "paid": False}
    row = res.data[0]
    if (row.get("status") or "") == "paid":
        return {"ok": True, "available": True, "referred": True, "paid": True,
                "reward": _safe_int(row.get("reward"), 0), "detail": "Награда уже начислена"}

    referred_row = _db_user(uid) or {}
    reward = _referral_reward_for(referred_row)
    referrer_id = _safe_int(row.get("referrer_id"), None)
    if referrer_id is None:
        return {"ok": True, "available": True, "referred": True, "paid": False}
    new_balance = award_coins(referrer_id, reward)
    if new_balance is None:
        raise HTTPException(status_code=503, detail="Не удалось начислить награду за реферала")
    try:
        supabase.table(REFERRALS_TABLE).update({
            "reward": reward, "status": "paid", "paid_at": _now_iso(),
        }).eq("referred_id", uid).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось отметить реферальную награду: %s", exc)
    return {"ok": True, "available": True, "referred": True, "paid": True,
            "reward": reward, "referrer_balance": new_balance,
            "detail": f"Пригласивший получил {reward} коинов"}


@app.post("/api/referrals")
async def referrals_info(data: UserAuth):
    uid = _auth_user_id(data.initData)
    stats = _referral_stats(uid)
    stats.update({"ok": True, "code": f"ref_{uid}"})
    return stats


# ---------- Telegram Stars ----------
@app.post("/api/stars_invoice")
async def stars_invoice(data: StarsInvoiceRequest):
    """Счёт на покупку коинов за Telegram Stars.

    Прайс — заглушка (STARS_PACKAGES). Пока STARS_ENABLED выключен, эндпоинт
    отдаёт список пакетов и честный «пока недоступно», чтобы фронт мог
    показать витрину без ошибок.
    """
    uid = _auth_user_id(data.initData)
    package = next((p for p in STARS_PACKAGES if p["id"] == data.package_id), None)
    if not STARS_ENABLED:
        return {"ok": False, "enabled": False, "packages": STARS_PACKAGES,
                "detail": "Покупка коинов за Stars скоро появится"}
    if package is None:
        raise HTTPException(status_code=400, detail="Неизвестный пакет коинов")
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink"
    payload = {
        "title": f"{package['coins']} коинов",
        "description": f"Пакет {package['coins']} коинов в AURA GAME",
        "payload": f"coins:{package['id']}:{uid}",
        "currency": "XTR",
        # Для Stars цена передаётся в самом «звёздном» номинале, без *100.
        "prices": [{"label": f"{package['coins']} коинов", "amount": int(package["stars"])}],
    }
    try:
        result = await _http_json(url, method="POST", payload=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Telegram недоступен, попробуйте позже") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        detail = (result or {}).get("description") if isinstance(result, dict) else ""
        raise HTTPException(status_code=503, detail=detail or "Не удалось создать счёт")
    return {"ok": True, "enabled": True, "url": result.get("result"),
            "coins": package["coins"], "stars": package["stars"]}


@app.get("/api/stars_packages")
async def stars_packages():
    return {"enabled": STARS_ENABLED, "packages": STARS_PACKAGES, "currency": "XTR"}


def stars_credit(user_id, coins, charge_id, stars: int = 0) -> Optional[int]:
    """Начислить коины за оплаченный Stars-инвойс ровно один раз.

    Вызывается из бота по successful_payment. Ключ идемпотентности —
    telegram_payment_charge_id: Telegram может повторить апдейт, а деньги
    игрок должен получить один раз.
    """
    uid = _safe_int(user_id, None)
    if uid is None or not charge_id:
        return None
    try:
        existing = (supabase.table(STARS_PAYMENTS_TABLE).select("charge_id")
                    .eq("charge_id", str(charge_id)).limit(1).execute())
        if existing.data:
            row = _db_user(uid)
            return _safe_int((row or {}).get("coins"), 0)
        supabase.table(STARS_PAYMENTS_TABLE).insert({
            "charge_id": str(charge_id), "user_id": uid, "coins": int(coins),
            "stars": int(stars), "created_at": _now_iso(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("stars_payments недоступна: %s", exc)
        return None
    return award_coins(uid, int(coins))


# ---------- Админ-API (то же, что показывает бот) ----------
@app.post("/api/admin/settings")
async def admin_settings(data: UserAuth):
    _require_admin_api(data.initData)
    values = _load_game_settings(force=True)
    return {
        "ok": True,
        "settings": values,
        "effective": {
            "upgrade_chance_factor": game_setting_float("upgrade_chance_factor", 1.0),
            "upgrade_house_edge": game_setting_float("upgrade_house_edge", UPGRADE_HOUSE_EDGE_FACTOR),
            "crash_house_edge": game_setting_float("crash_house_edge", 1.0),
        },
        "admin_only_app": ADMIN_ONLY_APP,
    }


@app.post("/api/admin/set_setting")
async def admin_set_setting(data: AdminSettingRequest):
    _require_admin_api(data.initData)
    ok = game_setting_set(data.key, data.value)
    if not ok:
        raise HTTPException(status_code=503, detail="Не удалось сохранить настройку")
    return {"ok": True, "key": data.key, "value": data.value}


@app.post("/api/admin/give_coins")
async def admin_give_coins(data: AdminGiveRequest):
    admin_id = _require_admin_api(data.initData)
    if data.amount == 0:
        raise HTTPException(status_code=400, detail="Сумма должна быть не нулевой")
    key = _idempotency_key(admin_id, data.request_id, "admin_give")
    cached = idempotency_lookup(key)
    if cached is not None:
        return cached
    coins = award_coins(data.user_id, data.amount)
    if coins is None:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    payload = {"ok": True, "user_id": data.user_id, "amount": data.amount, "new_balance": coins}
    idempotency_store(key, admin_id, "admin_give", payload)
    return payload


@app.post("/api/admin/ban")
async def admin_ban(data: AdminBanRequest):
    _require_admin_api(data.initData)
    try:
        if data.banned:
            supabase.table("bans").upsert({"user_id": data.user_id}).execute()
        else:
            supabase.table("bans").delete().eq("user_id", data.user_id).execute()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=_db_error_detail(exc, "Не удалось изменить бан")) from exc
    return {"ok": True, "user_id": data.user_id, "banned": bool(data.banned)}


@app.post("/api/admin/stats")
async def admin_stats(data: UserAuth):
    """Сколько поставлено и сколько выплачено — по логам, которые уже есть."""
    _require_admin_api(data.initData)
    stats = {"users": 0, "coins_in_system": 0, "upgrade_bets": 0, "upgrade_paid": 0,
             "upgrade_spins": 0, "crash_rounds": 0, "settings": _load_game_settings(force=True)}
    try:
        users = supabase.table("users").select("user_id, coins").execute().data or []
        stats["users"] = len(users)
        stats["coins_in_system"] = sum(_safe_int(row.get("coins"), 0) for row in users)
    except Exception:
        pass
    try:
        log = supabase.table("upgrade_log").select("amount_from, win_amount, item_from").execute().data or []
        stats["upgrade_spins"] = len(log)
        stats["upgrade_bets"] = sum(
            _safe_int(row.get("amount_from"), 0) * (UPGRADE_ITEM_PRICES.get(row.get("item_from"), 1))
            for row in log)
        stats["upgrade_paid"] = sum(_safe_int(row.get("win_amount"), 0) for row in log)
    except Exception:
        pass
    try:
        stats["crash_rounds"] = len(supabase.table("crash_history").select("round_id").execute().data or [])
    except Exception:
        pass
    return {"ok": True, **stats}



# ==================== ОТНОШЕНИЯ (ЛЮБОВЬ) ====================
# Таблицы: relationships (одна строка на пару) и love_stage_rewards (какие
# этапы уже оплачены). РП-действия и начисление Love коинов делает бот —
# в Mini App показывается плашка «В отношениях» и прогресс этапа.
RELATIONSHIPS_TABLE = "relationships"


def _love_relationship_for(user_id):
    """Активные отношения игрока или None. Ошибка базы = None (не ломаем профиль)."""
    try:
        res = (supabase.table(RELATIONSHIPS_TABLE).select("*")
               .eq("status", "active").execute())
    except Exception:
        return None
    for row in (res.data or []):
        if str(row.get("user_a")) == str(user_id) or str(row.get("user_b")) == str(user_id):
            return row
    return None


def _love_partner_id(row, user_id):
    a = _safe_int(row.get("user_a"), None)
    b = _safe_int(row.get("user_b"), None)
    if a is not None and str(a) == str(user_id):
        return b
    return a


def _love_partner_name(partner_id):
    row = _db_user(partner_id) if partner_id else None
    if not row:
        return f"id{partner_id}"
    return str(row.get("username") or f"id{partner_id}")


def love_state_for(user_id) -> dict:
    """Состояние отношений: партнёр, Love коины, этап и прогресс до следующего."""
    row = _love_relationship_for(user_id)
    if not row:
        return {"ok": True, "in_relationship": False, "available": True,
                "stages": LOVE_STAGES, "action_bonus": LOVE_ACTION_BONUS,
                "cooldown_hours": round(LOVE_ACTION_COOLDOWN / 3600, 1),
                "ice_cost": LOVE_ICE_COST, "rpc_actions": list(RP_ACTIONS)}
    score = _safe_int(row.get("love"), 0)
    partner_id = _love_partner_id(row, user_id)
    return {
        "ok": True,
        "available": True,
        "in_relationship": True,
        "partner_id": partner_id,
        "partner_name": _love_partner_name(partner_id),
        "love": score,
        "bank": _safe_int(row.get("bank"), 0),
        "stage": love_stage_info(score),
        "next_stage": love_next_stage(score),
        "action_bonus": LOVE_ACTION_BONUS,
        "cooldown_hours": round(LOVE_ACTION_COOLDOWN / 3600, 1),
        "ice_cost": LOVE_ICE_COST,
        "rpc_actions": list(RP_ACTIONS),
    }


@app.post("/api/love_state")
async def love_state(data: UserAuth):
    """Плашка «В отношениях» и прогресс этапа для профиля Mini App."""
    uid = _auth_user_id(data.initData)
    state = love_state_for(uid)
    if state.get("in_relationship"):
        # Страховка: если пара перешагнула этап, а бот не успел начислить
        # награду (например, действие пришло без интернета), наградим здесь.
        row = _love_relationship_for(uid)
        if row:
            granted = claim_stage_rewards(supabase, row)
            if granted:
                state["stage_rewards"] = granted
    return state

@app.get("/api/global_top")
async def get_global_top():
    res = supabase.table("users").select("user_id, username, coins").order("coins", desc=True).limit(50).execute()
    filtered = [u for u in res.data if int(u["user_id"]) not in ADMIN_IDS]
    return filtered[:50]


@app.get("/api/season_end")
async def get_season_end():
    # Лидерборд продлён до 02.11.2026, 17:00 по Москве (UTC+3).
    # Время задаём с явной зоной: Render живёт в UTC, и наивный datetime
    # сдвигал бы конец сезона на 3 часа.
    msk = datetime.timezone(datetime.timedelta(hours=3))
    end_date = datetime.datetime(2026, 11, 2, 17, 0, 0, tzinfo=msk)
    return {"end_timestamp": int(end_date.timestamp()), "timezone": "Europe/Moscow"}


@app.get("/{filename}", include_in_schema=False)
async def root_static_redirect(filename: str):
    """Корневые ссылки на файлы Mini App → в /miniapp/.

    Регистрируется ПОСЛЕДНИМ и работает только по белому списку, поэтому
    /api/* и /miniapp/* он не перекрывает. Нужен из-за самой частой ошибки
    настройки: в BotFather указывают корень сервиса или /index.html, а Mini App
    живёт в /miniapp/ — и игрок видел просто «Not Found».
    """
    name = (filename or "").strip()
    # /miniapp без слэша: catch-all перехватывает его раньше Mount, поэтому
    # редирект на канонический /miniapp/ делаем здесь же.
    if name == "miniapp":
        return RedirectResponse(url="/miniapp/", status_code=307)
    if name in ROOT_STATIC_FILES:
        return RedirectResponse(url=f"/miniapp/{name}", status_code=307)
    raise HTTPException(status_code=404, detail="Not Found")


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Понятный 404 вместо «Not Found» без подсказок.

    Ошибка на /api/* остаётся машинно-читаемой (detail), а на «человеческих»
    адресах добавлены подсказки: где лежит Mini App и какие пути существуют.
    """
    detail = getattr(exc, "detail", None) or "Not Found"
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=404, content={"detail": detail})
    return JSONResponse(status_code=404, content={
        "detail": detail,
        "path": request.url.path,
        "hint": "Mini App раздаётся по /miniapp/ — в BotFather укажите "
                "https://<домен-сервиса>/miniapp/",
        "miniapp": "/miniapp/",
        "api_health": "/api/health",
        "api_docs": "/api/docs",
    })


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))