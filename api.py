from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import uuid
import logging
import threading, os, hashlib, hmac, json, time, random, secrets, uvicorn, datetime, math, urllib.request, urllib.parse
from urllib.parse import parse_qsl
from dotenv import load_dotenv
from supabase import create_client, Client
from typing import Optional, Union
from db_schema import audit as db_schema_audit
from shop_catalog import SHOP_ITEMS, ShopPurchaseError, is_admin_id, parse_inventory, purchase_shop_item, \
    serialize_shop_items, missing_stock_rows, shop_stock_snapshot, stock_diagnostics, \
    sold_out_items, STOCK_LIMITS_ENABLED
import player_stats
from love import (LOVE_ACTION_BONUS, LOVE_ICE_COST, action_progress_text,
                  love_cooldown_seconds,
                  RP_ACTIONS, SOLO_CURRENCY, claim_solo_stage_rewards,
                  claim_stage_rewards, public_stage as love_public_stage,
                  public_stages as love_public_stages, stage_reached_text,
                  solo_state as love_solo_state)

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
API_BUILD = "2026-10-27-task77-shared-cooldown-history"

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

MINIAPP_INDEX = os.path.join(MINIAPP_DIR, "index.html")
MINIAPP_READY = os.path.isfile(MINIAPP_INDEX)


def miniapp_ready() -> bool:
    """Есть ли index.html ПРЯМО СЕЙЧАС.

    Не константа на старте: если папку miniapp доложили позже (повторный
    деплой, монтирование диска), страница должна начать открываться без
    перезапуска сервиса. Проверка — один os.path.isfile на запрос.
    """
    return os.path.isfile(MINIAPP_INDEX)

if os.path.isdir(MINIAPP_DIR):
    # То же самое, что app.mount("/miniapp", StaticFiles(directory="miniapp", html=True)),
    # только путь абсолютный: на Render рабочий каталог процесса может отличаться от корня репозитория.
    app.mount("/miniapp", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
else:  # pragma: no cover — защита от кривого деплоя
    logger.error("Папка miniapp не найдена (%s): Mini App не будет раздаваться", MINIAPP_DIR)

# В лог при старте — одной строкой всё, что нужно для диагноза «Not Found»:
# откуда взят путь, есть ли index.html, что лежит рядом и какой рабочий каталог.
if MINIAPP_READY:
    logger.info("Mini App: %s (index.html найден, %.1f КБ)",
                MINIAPP_DIR, os.path.getsize(MINIAPP_INDEX) / 1024)
else:  # pragma: no cover
    logger.error("Mini App НЕ будет раздаваться: нет %s. Рабочий каталог %s, рядом: %s",
                 MINIAPP_INDEX, os.getcwd(),
                 ", ".join(sorted(os.listdir(BASE_DIR))[:20]) or "пусто")


@app.api_route("/miniapp", methods=["GET", "HEAD"], include_in_schema=False)
async def miniapp_no_slash():
    """`/miniapp` без слэша → сразу отдаём страницу (а не 307).

    BotFather и игроки пишут адрес как придётся. 307 формально правильный, но
    в WebView Telegram лишний редирект — это лишний шанс получить «Not Found»
    и потерять initData, поэтому отдаём index.html сразу.
    """
    return _miniapp_page()


MINIAPP_BASE_TAG = b'<base href="/miniapp/">'
_MINAPP_HTML_CACHE = {"mtime": None, "html": None}


def _miniapp_html() -> bytes:
    """index.html с `<base href="/miniapp/">`.

    Зачем base: в index.html ВСЕ пути относительные (`style.css?v=…`,
    `app.js?v=…`, `cap_shop_ruby.png`). Если страницу открыли с корня сервиса
    (`https://aura-api…/`), относительные пути ушли бы в /style.css и
    /cap_shop_ruby.png — то есть мимо папки miniapp. С тегом base они всегда
    разрешаются в /miniapp/…, и одинаково работают оба адреса: и `/`, и
    `/miniapp/`. Файл читаем один раз и обновляем, если он изменился.
    """
    try:
        mtime = os.path.getmtime(MINIAPP_INDEX)
    except OSError:  # pragma: no cover
        return b""
    if _MINAPP_HTML_CACHE["html"] is not None and _MINAPP_HTML_CACHE["mtime"] == mtime:
        return _MINAPP_HTML_CACHE["html"]
    with open(MINIAPP_INDEX, "rb") as handle:
        html = handle.read()
    if b"<base " not in html:
        lowered = html.lower()
        position = lowered.find(b"<head>")
        if position >= 0:
            cut = position + len(b"<head>")
            html = html[:cut] + MINIAPP_BASE_TAG + html[cut:]
        else:  # pragma: no cover
            html = MINIAPP_BASE_TAG + html
    _MINAPP_HTML_CACHE.update({"mtime": mtime, "html": html})
    return html


def _miniapp_page() -> Response:
    """index.html Mini App с надёжными заголовками и работающими путями.

    Отдаём СТРАНИЦУ (а не 307-редирект): в WebView Telegram лишний редирект —
    это лишний шанс получить «Not Found», а с тегом `<base href="/miniapp/">`
    страница корректно грузит CSS, JS и картинки и с корня, и из /miniapp/.
    """
    if not miniapp_ready():  # pragma: no cover — битый деплой
        raise HTTPException(status_code=503, detail=(
            "Mini App не собран: в сервисе нет папки miniapp/index.html. "
            "Проверьте, что деплой идёт из корня репозитория (там лежат api.py и miniapp/)."))
    return Response(
        content=_miniapp_html(),
        media_type="text/html; charset=utf-8",
        headers={
            # Страница обязана перепроверяться: иначе WebView неделями держит
            # старый index.html и старые ?v= у app.js/style.css.
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ==== Аварийный режим статики ====
# Если StaticFiles почему-то не смонтировался (папки miniapp нет на момент
# старта, кривой деплой), Mini App всё равно должен открыться: отдаём файлы
# вручную из BASE_DIR/miniapp. Проверяем существование папки на КАЖДЫЙ запрос,
# поэтому файл, появившийся после старта, тоже подхватится.
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
}


def _miniapp_file_response(rel_path: str) -> Optional[Response]:
    """Отдать файл из miniapp/ напрямую (безопасно: только внутри папки)."""
    relative = (rel_path or "").strip("/")
    if not relative or relative == "index.html":
        return _miniapp_page()
    if ".." in relative.split("/") or relative.startswith("/"):
        return None
    absolute = os.path.normpath(os.path.join(MINIAPP_DIR, relative))
    if os.path.commonpath([absolute, MINIAPP_DIR]) != MINIAPP_DIR:
        return None
    if not os.path.isfile(absolute):
        return None
    extension = os.path.splitext(absolute)[1].lower()
    cache = ("no-cache, no-store, must-revalidate" if extension == ".html"
             else "public, max-age=86400")
    return FileResponse(absolute, media_type=STATIC_TYPES.get(extension,
                                                              "application/octet-stream"),
                        headers={"Cache-Control": cache})


MINIAPP_CANONICAL_URL = (os.getenv("AURA_WEBAPP_URL") or os.getenv("WEBAPP_URL") or "").strip()


def _canonical_miniapp_url(request) -> str:
    """Внешний адрес Mini App — тот, который открывает Telegram.

    Задача 42. Причина «Not Found» по кнопке Mini App: у сервиса Render два
    хоста — публичный https://<service>.onrender.com (443) и внутренний
    http://<service>:<PORT>. Если в BotFather / в кнопке записан адрес вида
    https://<service>-<port>.onrender.com или http://…:$PORT, Telegram идёт на
    порт, который наружу не опубликован, и Render отвечает «Not Found».

    Поэтому Mini App всегда строит ссылку из канонического адреса: переменная
    AURA_WEBAPP_URL (если задана), иначе RENDER_EXTERNAL_URL, иначе origin
    текущего запроса БЕЗ порта.
    """
    if MINIAPP_CANONICAL_URL:
        base = MINIAPP_CANONICAL_URL.rstrip("/")
        if not base.endswith("/miniapp"):
            base = f"{base}/miniapp"
        return base + "/"
    env_base = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if env_base:
        if not env_base.endswith("/miniapp"):
            env_base = f"{env_base}/miniapp"
        return env_base + "/"
    try:
        # Render присылает публичный хост в X-Forwarded-Host; внутри контейнера
        # request.headers["host"] может быть внутренним (0.0.0.0:$PORT).
        host = (request.headers.get("x-forwarded-host")
                or request.headers.get("host") or request.url.netloc)
    except Exception:  # noqa: BLE001
        host = ""
    scheme = "https"
    try:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        if forwarded in ("http", "https"):
            scheme = forwarded
        elif request.url.scheme in ("http", "https"):
            scheme = request.url.scheme
    except Exception:  # noqa: BLE001
        pass
    host = _host_without_port(host)          # порт наружу не публикуется
    if not host:
        host = "localhost"
    return f"{scheme}://{host}/miniapp/"


def _host_without_port(host: str) -> str:
    return str(host or "").split(",")[0].strip().split(":")[0]


def _canonical_host() -> str:
    """Публичный хост сервиса из настроек Render (без пути и без порта)."""
    raw = (MINIAPP_CANONICAL_URL or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    if not raw:
        return ""
    raw = raw.split("://")[-1].split("/")[0]
    return raw.split(":")[0].strip().lower()


@app.middleware("http")
async def miniapp_canonical_redirect(request: Request, call_next):
    """Mini App не отдаём по внутреннему адресу с портом (задача 42).

    «Not Found» по кнопке Mini App появлялся, когда ссылка вела на внутренний
    адрес Render (`http://<service>:<port>` / `https://<service>-<port>...`):
    наружу опубликован только 443. Если в хосте запроса есть нестандартный
    порт и известен публичный адрес сервиса — переносим туда.

    Условия жёсткие, чтобы не сломать ничего лишнего:
    * нужен известный публичный хост (AURA_WEBAPP_URL или RENDER_EXTERNAL_URL);
    * в хосте запроса должен быть явный порт, отличный от 80/443;
    * если хост уже канонический — редиректа нет (петли невозможны);
    * запросы к /api/* не трогаем вообще.
    """
    path = request.url.path
    if not (path == "/miniapp" or path.startswith("/miniapp/") or path == "/index.html"):
        return await call_next(request)
    canonical = _canonical_host()
    if not canonical:
        return await call_next(request)          # локальная разработка — как есть
    host = str(request.headers.get("x-forwarded-host")
               or request.headers.get("host") or "").split(",")[0].strip().lower()
    name, _, port = host.partition(":")
    # Локальную разработку не трогаем: у неё всегда «нестандартный» порт.
    if name in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "::1"):
        return await call_next(request)
    if not port or port.strip() in ("80", "443") or name == canonical:
        return await call_next(request)
    target = _canonical_miniapp_url(request)
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=target, status_code=307)


@app.api_route("/miniapp/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def miniapp_files(path: str):
    """Дублирующий обработчик файлов Mini App — на случай, если mount не встал.

    Когда StaticFiles смонтирован, до этой функции дело не доходит (mount
    перехватывает /miniapp/* раньше). Если же папки на старте не было, mount
    пропущен — и этот обработчик всё равно отдаст index.html, app.js, style.css
    и картинки, вместо «Not Found».
    """
    response = _miniapp_file_response(path)
    if response is not None:
        return response
    raise HTTPException(status_code=404, detail=f"Файл {path} не найден в miniapp/")


def _wants_health_json(request: Request) -> bool:
    """Это запрос монитора/health-check, а не игрока?

    Render проверяет сервис запросом **HEAD /** со своим User-Agent, UptimeRobot
    и прочие мониторы бьют в корень и просят `Accept: */*`. Таким клиентам нужен
    дешёвый JSON {"ok": true, "status": "alive"}, а не HTML: по HTML монитор с
    keyword `alive` покажет «Down».

    Игрок приходит из Telegram WebView (Accept содержит text/html, свой
    User-Agent) или просто открывает адрес в браузере — ему отдаём Mini App.
    """
    if request.method.upper() == "HEAD":
        return True                       # HEAD / — это Render health check
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return False                      # браузер и WebView Telegram
    if not accept or accept.strip() == "*/*":
        user_agent = (request.headers.get("user-agent") or "").lower()
        browsers = ("mozilla", "chrome", "safari", "firefox", "edg", "telegram")
        return not any(name in user_agent for name in browsers)
    return True


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_redirect(request: Request):
    """Корень сервиса: игроку — Mini App, мониторингу — «alive».

    Задача 38: раньше здесь был только `@app.get("/")`, поэтому **HEAD /** от
    Render health-check отвечал `405 Method Not Allowed` — сервис не проходил
    проверку и ссылка на корень открывалась как «Not Found».

    Теперь:
    * HEAD / (Render) → 200 и `{"ok": true, "status": "alive"}`;
    * браузер / WebView Telegram (Accept: text/html) → index.html Mini App;
    * curl/монитор с `Accept: */*` → тот же alive-JSON, что /api/ping.
    """
    if _wants_health_json(request):
        return await ping()
    return _miniapp_page()


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


@app.api_route("/api/ping", methods=["GET", "HEAD"])
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


@app.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
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


@app.api_route("/api/health", methods=["GET", "HEAD"])
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
            # ready=false — на сервисе нет miniapp/index.html: Mini App не собран,
            # и корень сайта отвечает 503 с человеческим текстом. Лечится
            # редеплоем из корня репозитория (api.py и miniapp/ лежат рядом).
            "ready": miniapp_ready(),
            "url": "/miniapp/",
            "root_url": "/",
            "index_path": MINIAPP_INDEX,
            "mount": "StaticFiles /miniapp + аварийный обработчик /miniapp/{path}",
            "health_check": {"method": "HEAD /", "expect": 200},
        },
        # Диагностика схемы: админ открывает /api/db_audit (или пишет боту «бд»)
        # и видит, каких миграций/колонок не хватает, с готовым SQL.
        "db_audit": {"endpoint": "/api/db_audit", "bot_command": "бд"},
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
        # Оба рубильника объявлены ниже по файлу, но к моменту запроса уже есть.
        "maintenance": MAINTENANCE_MODE or app_closed_now(),
        # Доступ: пока игра закрыта (app_closed), Mini App открывают только админы.
        "access": {
            "admin_only_app": app_closed_now(),
            "admins": len(ADMIN_IDS),
            "message": ADMIN_ONLY_MESSAGE if app_closed_now() else "",
        },
        # Экономика задачи 25: по этим числам видно нужную сборку на сервисе.
        "economy": {
            "subscription_bonus": SUBSCRIPTION_BONUS,
            "channel": f"@{CHANNEL_USERNAME}",
            "passive_rates": PASSIVE_INCOME_RATES,
            "passive_max_hours": PASSIVE_INCOME_MAX_HOURS,
            # Пассивка идёт всегда (апка закрыта — копится), ночью по МСК вдвое меньше.
            "passive_night": {"multiplier": PASSIVE_NIGHT_MULTIPLIER,
                              "from": PASSIVE_NIGHT_START_HOUR, "to": PASSIVE_NIGHT_END_HOUR},
            "referral_rewards": {
                "base": REFERRAL_REWARD_BASE,
                "nft2": REFERRAL_REWARD_NFT2,
                "premium": REFERRAL_REWARD_PREMIUM,
            },
            "stars_enabled": STARS_ENABLED,
            "upgrade": _upgrade_economy_config(),
            # Задача 31: арен две и они идут параллельно; mode оставлен для совместимости.
            "arena_mode": _arena_mode(),
            "arena_views": list(ARENA_VIEWS),
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


# Задача 62: Mini App ОТКРЫТА ВСЕМ.
#
# История: задача 44 пускала всех, задача 57 срочно закрыла доступ «до админов»
# (ADMIN_ONLY_APP по умолчанию 1) — и после каждого деплоя без переменной
# окружения обычные игроки видели экран «Технические работы». Задача 61 добавила
# рубильник из бота, задача 62 его убрала вместе с закрытым дефолтом.
#
# Теперь: доступ открыт всегда, никаких флагов в базе и env на пути игрока нет.
# Остался только `require_play_access` — единая точка входа, которая сейчас
# никого не блокирует (кроме бана, который проверяется отдельно).
ADMIN_ONLY_APP = False
ADMIN_ONLY_MESSAGE = ""
# Оставлено для совместимости со старыми сборками Mini App и админ-API:
# «приложение закрыто» теперь всегда False.
APP_CLOSED_SETTING_KEY = "app_closed"


# Задача 71 («поставь всё на техработы»): тумблер вернулся, но уже как
# ОСОЗНАННОЕ решение админа, а не как забытый флаг в окружении Render.
# Значение живёт одной строкой в game_settings, читается с кэшем 15 секунд
# (см. maintenance_now) и НИКОГДА не включается по умолчанию: нет строки —
# игра открыта всем. Включить и выключить можно из Mini App (админ-панель) или
# командой бота `техработы вкл` / `техработы выкл`.
MAINTENANCE_SETTING_KEY = "maintenance_mode"
_MAINTENANCE_CACHE = {"value": False, "at": 0.0}
MAINTENANCE_CACHE_TTL = 15.0


def maintenance_now() -> bool:
    """Включены ли техработы прямо сейчас (только админ может их включить).

    Читаем из game_settings с коротким кэшем: иначе каждый запрос игрока бил бы
    в базу. Если база недоступна — остаётся последнее известное значение, но
    НЕ «включено по умолчанию»: упавшая база не должна запирать игру.
    """
    now = time.time()
    if (now - float(_MAINTENANCE_CACHE["at"] or 0)) < MAINTENANCE_CACHE_TTL:
        return bool(_MAINTENANCE_CACHE["value"])
    value = bool(_MAINTENANCE_CACHE["value"])
    try:
        row = _load_game_settings().get(MAINTENANCE_SETTING_KEY)
        if row is not None:
            value = str(row).strip().lower() in ("1", "true", "yes", "on", "вкл")
            if str(row).strip().lower() in ("0", "false", "no", "off", "выкл", ""):
                value = False
    except Exception:  # noqa: BLE001 — не смогли прочитать, работаем как раньше
        pass
    _MAINTENANCE_CACHE.update({"value": value, "at": now})
    return value


def set_maintenance(enabled: bool) -> bool:
    """Включить/выключить техработы. True — настройка записана в базу."""
    ok = game_setting_set(MAINTENANCE_SETTING_KEY, "1" if enabled else "0")
    _MAINTENANCE_CACHE.update({"value": bool(enabled), "at": time.time()})
    # Настройки кэшируются на 30 с — после записи сбрасываем кэш, иначе админ
    # переключит тумблер и 30 секунд будет видеть старое значение.
    _game_settings_cache.update({"at": 0.0, "values": {}})
    return bool(ok)


def app_closed_now() -> bool:
    """«Приложение закрыто для всех, кроме админов» — это и есть техработы.

    Задача 62 убрала закрытый дефолт (после деплоя игра открывалась не у всех),
    задача 71 вернула рубильник, но только как явное действие админа. Нет
    строки в game_settings — False, то есть игра открыта всем.
    """
    return maintenance_now()


def set_app_closed(closed: bool) -> bool:
    """Совместимость со старыми вызовами: то же, что set_maintenance."""
    return set_maintenance(closed)


# ==================== ПОДПИСКА НА КАНАЛ ====================
# Бонус начисляется РОВНО ОДИН РАЗ за всё время: факт начисления хранится в
# таблице subscriptions_history (user_id — первичный ключ). Отписался и
# подписался снова — бонуса не будет.
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "Auragameplay_Channel").strip().lstrip("@")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"
SUBSCRIPTION_BONUS = 200
SUBSCRIPTIONS_TABLE = "subscriptions_history"
# Задача 35: выдачу бонуса делает БОТ по кнопке «Проверить подписку» в личке
# (bot.py → cb_sub_check), а aura-api только присылает задание
# (/api/channel_task) и отдаёт статус (/api/channel_status). Правило задачи 33
# осталось: если getChatMember недоступен (бот не админ канала / Telegram
# молчит), бонус всё равно выдаётся — один раз за всё время
# (subscriptions_history, user_id — первичный ключ).
# CHANNEL_BONUS_STRICT оставлен для совместимости и больше не используется.
CHANNEL_BONUS_STRICT = _env_flag("CHANNEL_BONUS_STRICT", False)

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

# Пассивка капает ВСЕГДА, даже когда Mini App закрыт: сервер считает доход по
# прошедшему времени от отметки last_at, а игрок забирает накопленное при
# следующем заходе (в том числе за несколько дней — в пределах MAX_HOURS).
# Ночь (22:00–08:00 по Москве) идёт вдвое дешевле: за эти часы капает 50 %.
PASSIVE_NIGHT_START_HOUR = 22
PASSIVE_NIGHT_END_HOUR = 8
PASSIVE_NIGHT_MULTIPLIER = 0.5
PASSIVE_DAY_MULTIPLIER = 1.0
MSK_OFFSET_SECONDS = 3 * 3600            # Москва — UTC+3, круглый год
DAY_SECONDS = 86400.0

# ==================== РЕФЕРАЛЬНАЯ СИСТЕМА ====================
REFERRALS_TABLE = "referrals"
REFERRAL_REWARD_BASE = 500          # обычный реферал
REFERRAL_REWARD_NFT2 = 800          # реферал с 2+ NFT
REFERRAL_REWARD_PREMIUM = 2000      # premium с рейтингом и 1+ NFT
# «NFT» в этом проекте — лимитированные капсулы (их тираж конечен, как у NFT),
# а всё, что начинается с nft, тоже считается. Premium — колонка users.premium
# (если её нет, реферал считается обычным).
REFERRAL_NFT_ITEMS = {"galactic", "diamond"}

# Имя бота для реферальных ссылок. Раньше Mini App подставлял в ссылку
# <code>initDataUnsafe.bot_username</code> — то есть имя ТОГО бота, из которого
# страницу открыли. У проекта есть старый бот, поэтому игроки получали ссылку
# на чужого бота. Теперь имя берём на сервере и один раз: env BOT_USERNAME →
# Telegram getMe по токену настоящего бота → константа.
BOT_USERNAME_OVERRIDE = (os.getenv("BOT_USERNAME")
                         or os.getenv("AURA_BOT_USERNAME") or "").strip().lstrip("@")
DEFAULT_BOT_USERNAME = "AuragameBot"
_BOT_USERNAME_CACHE = {"value": "", "at": 0.0}

# ==================== TELEGRAM STARS (донат) ====================
# Пакеты коинов за Telegram Stars. Покупка ВКЛЮЧЕНА по умолчанию: кнопка «+»
# у баланса раньше показывала «Пополнение скоро!» и не начисляла ничего.
# Звёзды не требуют платёжного провайдера (currency = XTR), поэтому счёт
# создаётся сразу. Выключить можно переменной окружения STARS_ENABLED=0.
STARS_ENABLED = _env_flag("STARS_ENABLED", True)
# Задача 70: прайс заказчика (STAR PACKS) + бонусные коины за крупный донат.
# bonus — ДОПОЛНИТЕЛЬНЫЕ коины сверх пакета, их видно в витрине («+150»).
STARS_PACKAGES = [
    {"id": "stars_1", "stars": 1, "coins": 15, "bonus": 0},
    {"id": "stars_5", "stars": 5, "coins": 100, "bonus": 0},
    {"id": "stars_10", "stars": 10, "coins": 210, "bonus": 0},
    {"id": "stars_25", "stars": 25, "coins": 550, "bonus": 0},
    {"id": "stars_50", "stars": 50, "coins": 1150, "bonus": 150},
    {"id": "stars_100", "stars": 100, "coins": 2400, "bonus": 400},
    {"id": "stars_250", "stars": 250, "coins": 6250, "bonus": 1000},
    {"id": "stars_500", "stars": 500, "coins": 13000, "bonus": 2500},
    {"id": "stars_1000", "stars": 1000, "coins": 27500, "bonus": 5000},
]
STARS_PACKAGES_BY_ID = {p["id"]: p for p in STARS_PACKAGES}


def stars_package_total(package) -> int:
    """Сколько всего коинов придёт за пакет: цена + бонус."""
    if not package:
        return 0
    return int(package.get("coins") or 0) + int(package.get("bonus") or 0)


def stars_package_by_payload(payload: str):
    """Пакет по payload инвойса: «stars:stars_50:<uid>» или старый «coins:coins_5000:<uid>».

    Старые инвойсы должны оплачиваться: у них другой id, поэтому сначала ищем
    текущий прайс, а потом — совместимость по числу звёзд.
    """
    parts = str(payload or "").split(":")
    if len(parts) < 2:
        return None
    package = STARS_PACKAGES_BY_ID.get(parts[1])
    if package:
        return package
    # Старые инвойсы (задача 3): id — это «за сколько коинов» платили.
    # Цифра справа — сколько звёзд стоила покупка, по ней и находим пакет
    # текущего прайса: 1000 → 5★ (100 коинов), 5000 → 25★ (550),
    # 15000 → 50★ (1150 + 150 бонуса = 1300). Игрок получает не меньше,
    # чем ожидал, а не «инвойс не найден» после оплаты.
    legacy = {"coins_1000": 5, "coins_5000": 25, "coins_15000": 50}
    stars = legacy.get(parts[1])
    return next((p for p in STARS_PACKAGES if p["stars"] == stars), None)
STARS_PAYMENTS_TABLE = "stars_payments"

# ==================== IDEMPOTENCY ====================
# Ключ запроса от клиента (request_id) + таблица idempotency_keys: повторная
# отправка той же операции (двойной тап, ретрай после таймаута) не спишет
# деньги дважды, а вернёт сохранённый ответ.
IDEMPOTENCY_TABLE = "idempotency_keys"
# Сколько живёт сохранённый ответ. Через сутки ключ можно переиспользовать.
IDEMPOTENCY_TTL = 86400

# ==================== ТЕХРАБОТЫ ====================
# Задача 57: техработы ВКЛЮЧЕНЫ по умолчанию — играют только ADMIN_IDS
# (экран «Технические работы» + 403 на любые игровые действия, обойти запросом
# к API нельзя). Открыть игру = MAINTENANCE_MODE=0 и ADMIN_ONLY_APP=0 в Render.
# Апгрейд от этого флага НЕ зависит: он отдельно закрыт списком ADMIN_IDS
# (см. UPGRADE_ADMIN_ONLY ниже).
# Задача 65: старые переменные больше НЕ закрывают игру.
#
# Почему пришлось менять имена: в render.yaml на ветке main (по ней развёрнут
# сервис) жёстко прописаны MAINTENANCE_MODE=1, ADMIN_ONLY_APP=1 и
# ADMINS_ONLY_BOT=1 — значения от задачи 57. Blueprint при обновлении кода их не
# переписывает, поэтому новый код всё равно видел «1» и показывал экран
# «Технические работы» всем, кроме админов — на скрине заказчика это был старый
# текст «пока доступ есть лишь у админов, скоро откроем всем».
#
# ЗАДАЧА 67: аварийный стоп убран совсем. Заказчик трижды получал «Технические
# работы» после деплоя, и каждый раз виноват был очередной флаг окружения.
# Теперь аварийного стопа по доступу НЕТ: игра открыта всем и всегда, любые
# переменные (и старые, и новые) игнорируются. Бан игрока работает отдельно.
LEGACY_ACCESS_FLAGS = ("MAINTENANCE_MODE", "ADMIN_ONLY_APP")


def _legacy_access_flags_enabled() -> list:
    """Какие старые «закрывающие» переменные всё ещё стоят в единице."""
    return [name for name in LEGACY_ACCESS_FLAGS if _env_flag(name, False)]


def _maintenance_enabled() -> bool:
    """Аварийный стоп: читаем тумблер админа (задача 71).

    Переменные окружения по-прежнему НЕ влияют — рубильник один и он в базе.
    """
    return maintenance_now()


MAINTENANCE_MODE = False
# Задача 65: видно в логах Render, почему игра всё-таки открыта, хотя в
# окружении остались «закрывающие» переменные от задачи 57.
if _legacy_access_flags_enabled():
    logger.warning(
        "MAINTENANCE_MODE/ADMIN_ONLY_APP остались в окружении (%s), но НЕ "
        "закрывают игру: с задачи 67 доступ открыт всем и заблокировать его "
        "нельзя ни переменной, ни строкой в базе.",
        ", ".join(_legacy_access_flags_enabled()))
MAINTENANCE_MESSAGE = "🛠️ Технические работы: скоро вернёмся"


def can_play(user_id) -> bool:
    """Может ли этот пользователь играть прямо сейчас.

    Задача 67: «ОТКРОЙ АПКУ ЧТОБЫ ВСЕ ВСЕ МОГЛИ ВХОДИТЬ» — по умолчанию игра
    открыта всем и навсегда, никакие переменные окружения её не закрывают.

    Задача 71: админ может осознанно включить техработы («поставь всё на тех
    работы»). Тогда играют только ADMIN_IDS, остальные видят понятный экран.
    Бан проверяется отдельно и работает всегда.
    """
    uid = _safe_int(user_id, None)
    if uid is not None and is_admin_id(uid, ADMIN_IDS):
        return True
    return not maintenance_now()


def require_play_access(user) -> int:
    """Единая проверка входа в игру. Возвращает user_id, иначе — HTTPException."""
    user_id = _safe_int(user.get("id"), None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    if not can_play(user_id):
        raise HTTPException(status_code=403, detail=_access_denied_message(user_id))
    return user_id


def _gate_view(init_data: str = "") -> None:
    """Гейт для GET-ручек состояния игры (арена/краш/траектория).

    Задача 57: эти ручки вызывались без параметров и отдавали состояние всем
    подряд. Теперь нужен initData, и смотреть может только тот, кому можно
    играть. Пустой initData — это не Mini App, а прямой запрос из браузера:
    отказываем.
    """
    raw = (init_data or "").strip()
    if not raw:
        raise HTTPException(status_code=403, detail=MAINTENANCE_MESSAGE)
    user = verify_telegram_data(raw)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)


def require_play_access_uid(user_id) -> int:
    """То же, что require_play_access, но для ручек, где user_id уже получен.

    Задача 57: раньше часть эндпоинтов (арена/краш GET, апгрейд, пассивка,
    звёзды, рефералы, лав) проверяла только initData — во время техработ
    не-админ мог запросить состояние игры и начисления напрямую. Теперь у
    каждой такой ручки есть этот вызов.
    """
    uid = _safe_int(user_id, None)
    if uid is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    if not can_play(uid):
        raise HTTPException(status_code=403, detail=_access_denied_message(uid))
    return uid


def _access_denied_message(user_id) -> str:
    """Текст отказа. Задача 67: доступ открыт всем, поэтому сюда не попадают.

    Оставлено для совместимости со старыми клиентами и админ-API: если когда-то
    понадобится ответить отказом вручную, текст будет понятным.
    """
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
async def chest_ready(initData: str = ""):
    """Готова ли таблица сундуков (диагностика для бота и админа).

    Клиент получает «Сундуки обновляются, попробуйте чуть позже», когда
    chest_claims недоступна: не применена миграция или в PostgREST устарел кэш
    схемы. Эта ручка отвечает одной строкой, что именно не так, чтобы не гадать
    по логам Render.

    Задача 57: ручка беспарольная, поэтому открыта только админам (initData).
    Бот вызывает её, подписывая данные сам (bot.py → admin_probe_init_data).
    """
    _gate_view(initData)
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
# Названия для подсказок в отказах: игрок читает «Рубиновая капсула», а не «ruby».
UPGRADE_ITEM_TITLES = {"capsule": "Обычная капсула", "uniq_capsule": "Уникальная капсула",
                       "ruby": "Рубиновая капсула", "diamond": "Алмазная капсула",
                       "galactic": "Галактическая капсула", "malachite": "Малахитовая капсула",
                       "emerald": "Изумрудная капсула"}
UPGRADE_SOURCES = ("coin",) + tuple(UPGRADE_ITEM_PRICES)
UPGRADE_TARGETS = ("coin",) + tuple(UPGRADE_ITEM_PRICES)


# ==================== ИГРОВЫЕ МОМЕНТЫ ПРОФИЛЯ (задача 74) ====================
# Счётчики пишем «в фоне»: ответ игроку не должен ждать базу статистики, а
# сломанная статистика не должна ломать игру (см. player_stats.py).
def stats_note(user_id, **deltas) -> None:
    def _write():
        try:
            player_stats.add_stats(supabase, user_id, **deltas)
        except Exception:  # noqa: BLE001
            logger.debug("статистика: запись не удалась", exc_info=True)
    try:
        asyncio.create_task(asyncio.to_thread(_write))
    except RuntimeError:      # нет цикла событий (скрипты, тесты)
        _write()


def ledger_note(user_id, kind: str, amount: int, title: str = "", meta=None) -> None:
    """История ставок и пополнений для профиля (задача 77) — в фоне.

    Пишем тем же способом, что и статистику: ответ игроку не ждёт базу, а
    сломанная лента игру не ломает.
    """
    def _write():
        try:
            player_stats.add_ledger(supabase, user_id, kind, amount, title, meta)
        except Exception:  # noqa: BLE001
            logger.debug("история операций: запись не удалась", exc_info=True)
    try:
        asyncio.create_task(asyncio.to_thread(_write))
    except RuntimeError:
        _write()


def stats_event(user_id, kind: str, title: str, amount: int = 0, meta=None) -> None:
    def _write():
        try:
            player_stats.add_event(supabase, user_id, kind, title, amount, meta)
        except Exception:  # noqa: BLE001
            logger.debug("статистика: событие не записано", exc_info=True)
    try:
        asyncio.create_task(asyncio.to_thread(_write))
    except RuntimeError:
        _write()


def _upgrade_item_title(item_id) -> str:
    if not item_id:
        return ""
    if item_id == "coin":
        return "коины"
    return UPGRADE_ITEM_TITLES.get(item_id, str(item_id))


def _upgrade_moment(source: str, amount: int, target: str) -> tuple[str, int, int]:
    """(текст, ценность «из», ценность «в») — для «Лучшего апгрейда» в профиле."""
    from_value = _upgrade_source_value(source, amount)
    if target == "coin":
        to_value = from_value  # множитель считает сервер; ценность уточним ниже
        text = f"{_upgrade_item_title(source)} x{amount} → коины"
    else:
        to_value = UPGRADE_ITEM_PRICES.get(target, 0)
        text = f"{_upgrade_item_title(source)} x{amount} → {_upgrade_item_title(target)}"
    return text, from_value, to_value
# В апгрейде пока играют только админы — шанс честный. Когда откроем для всех,
# реальный шанс = визуальный / этот делитель (то есть x2 = 25%).
UPGRADE_NON_ADMIN_DIVIDER = 1
# Апгрейд закрывался для всех, кроме ADMIN_IDS (задача 21). Задача 44 (п.14):
# игра открыта ДЛЯ ВСЕХ, апгрейд больше не спрятан. Шанс при этом честный для
# всех — делитель ниже выключен, иначе новичок играл бы вдвое хуже админа.
# Закрыть снова: UPGRADE_ADMIN_ONLY=1 в Render.
# Задача 65: то же самое, что с техработами — старое имя UPGRADE_ADMIN_ONLY
# осталось в render.yaml со значением 1 и перекрывало открытый апгрейд.
# Отдельный стоп живёт под новым именем: AURA_UPGRADE_ADMIN_ONLY=1.
UPGRADE_ADMIN_ONLY = _env_flag("AURA_UPGRADE_ADMIN_ONLY", False)
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


def _random_arena_bot_count(arena: str = "normal"):
    """Сколько ботов добавляем за раунд в конкретном котле.

    Задача 43: в командной арене ботов должно быть «разы меньше» — максимум
    2–3 на всю игру, иначе круг превращается в ботов против одного игрока.
    """
    if arena == "team":
        return random.randint(TEAM_BOT_COUNT_MIN, TEAM_BOT_COUNT_MAX)
    # Keep most rounds light; large bot rooms are occasional.
    return random.randint(1, 3) if random.random() < 0.85 else random.randint(4, 10)


# ==================== ОБЩИЕ ХЕЛПЕРЫ ЭКОНОМИКИ ====================
def _primary_user_row(rows):
    """Строка профиля, которую видит ИГРОК: максимум коинов (как в /api/user).

    Задача 69: в базе бывают старые дубли users с одним user_id. Профиль
    показывает строку с максимумом коинов, а списание ставки брало res.data[0] —
    «мёртвую» строку с 0. Игрок видел 1 000 коинов, а игра отвечала
    «Недостаточно коинов»; если суммы хватало — коины уходили не с того баланса.
    Теперь везде, где читаем/пишем деньги, берём строку по одному правилу.
    """
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=lambda row: _safe_int(row.get("coins"), 0))


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
# created_at, …) или нет самой таблицы. PostgREST отвечает PGRST204/PGRST205/
# 42703 — раньше игрок видел «Внутренняя ошибка», а админ не понимал, что чинить.
#
# ВАЖНО (задача 27): раньше текст ошибки ВСЕГДА писал «в таблице users», даже
# если колонки не хватало в subscriptions_history или referrals. Из-за этого
# админ применял миграцию users, а ошибка возвращалась — и выглядело так, будто
# миграция «не применилась». Теперь имя таблицы берётся из ответа PostgREST.
_MIGRATION_HINT_BY_COLUMN = {
    "created_at": "supabase/migrations/20261028000000_created_at_and_crash_history.sql",
    "ice": "supabase/migrations/20261109000000_economy_social.sql",
    "premium": "supabase/migrations/20261109000000_economy_social.sql",
    "inventory": "supabase/migrations/20261109000000_economy_social.sql",
    "daily_buys": "supabase/migrations/20261027000000_shop_daily_limits.sql",
}
_POSTGREST_RELOAD_HINT = ("Если миграция уже применена — обновите кэш схемы: "
                          "SQL Editor → NOTIFY pgrst, 'reload schema'; "
                          "(Dashboard → Settings → API → Reload schema)")


def _sql_error_column_table(message: str) -> tuple:
    """Достаём (колонка, таблица) из текста ошибки PostgREST/Postgres.

    PostgREST: "Could not find the 'created_at' column of 'users' in the schema cache"
    Postgres:  column "created_at" of relation "users" does not exist
    """
    import re as _re
    text = (message or "").lower()
    column = None
    found = (_re.search(r"'([a-z_][a-z0-9_]*)'\s+column", text)
             or _re.search(r'column\s+"([a-z_][a-z0-9_]*)"', text)
             or _re.search(r"column\s+'?\"?([a-z_][a-z0-9_]*)", text))
    if found:
        column = found.group(1)
    table = None
    found_table = (_re.search(r"of\s+'([a-z_][a-z0-9_]*)'\s+in the schema cache", text)
                   or _re.search(r'of relation "([a-z_][a-z0-9_]*)"', text)
                   # "Could not find the table 'public.referrals' in the schema cache"
                   or _re.search(r"table\s+'([a-z_]+\.[a-z0-9_]+)'", text)
                   or _re.search(r"table\s+'([a-z_][a-z0-9_]*)'", text)
                   or _re.search(r'table "([a-z_]+\.[a-z0-9_]+)"', text)
                   or _re.search(r'table "([a-z_][a-z0-9_]*)"', text))
    if found_table:
        table = found_table.group(1).split(".")[-1]
    return column, table


def _db_error_detail(exc: Exception, fallback: str = "База временно недоступна") -> str:
    """Человеческий текст ошибки БД для фронтенда (его показывает tg.showAlert).

    Возвращает готовый `detail` для HTTPException: без SQL-простыней, но с
    конкретикой — какой колонки/таблицы не хватает, в КАКОЙ таблице и что применить.
    """
    message = f"{type(exc).__name__}: {exc}"
    lower = message.lower()

    # 1) Нет таблицы / устарел кэш схемы PostgREST.
    if "pgrst205" in lower or "42p01" in lower or "could not find the table" in lower:
        _, table = _sql_error_column_table(message)
        name = table or "нужная"
        return (f"Таблицы «{name}» нет в базе (или устарел кэш PostgREST). "
                "Примените миграции из supabase/migrations, затем выполните "
                "NOTIFY pgrst, 'reload schema'")

    # 2) Нет колонки: PostgREST: "Could not find the 'inventory' column of 'users'".
    if "pgrst204" in lower or "42703" in lower or "column" in lower:
        column, table = _sql_error_column_table(message)
        where = ["В базе не хватает"]
        if column:
            where.append(f"колонки «{column}»")
        where.append(f"в таблице «{table}»" if table else "колонки в одной из таблиц")
        detail = " ".join(where) + ". "
        migration = _MIGRATION_HINT_BY_COLUMN.get(column or "")
        if migration:
            detail += f"Примените {migration}. "
        else:
            detail += "Сверьте таблицу с supabase/migrations и добавьте колонку. "
        detail += _POSTGREST_RELOAD_HINT + ". Данные игрока не потеряны."
        return detail

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


def _optional_columns_error(exc: Exception, optional) -> bool:
    """True, если ошибка именно про одну из НЕобязательных колонок.

    Нужно для «мягкой» записи: created_at у служебных таблиц — украшение
    (по нему ничего не считается в игре), и из-за его отсутствия не должен
    ломаться бонус за подписку или реферал.
    """
    lower = f"{type(exc).__name__}: {exc}".lower()
    if not any(code in lower for code in ("pgrst204", "42703", "column")):
        return False
    column, _ = _sql_error_column_table(str(exc))
    return bool(column) and column in set(optional)


def _insert_tolerant(table: str, payload: dict, optional=("created_at",),
                     upsert: bool = False, on_conflict: str = None,
                     logger_prefix: str = ""):
    """INSERT/upsert, который не падает из-за отсутствия «украшающих» колонок.

    Сначала пишем полный payload. Если база ругается ровно на created_at
    (или другую необязательную колонку) — повторяем без неё. Так одноразовый
    бонус за подписку начисляется, даже если миграция применена не до конца,
    а в лог пишется предупреждение с именем таблицы.
    """
    def _run(data: dict):
        query = supabase.table(table)
        if upsert:
            return query.upsert(data, on_conflict=on_conflict).execute()
        return query.insert(data).execute()

    try:
        return _run(payload)
    except Exception as exc:  # noqa: BLE001
        if not _optional_columns_error(exc, optional):
            raise
        column, _ = _sql_error_column_table(str(exc))
        stripped = {key: value for key, value in payload.items() if key != column}
        logger.warning("%s%s: колонки «%s» нет — пишем без неё. %s",
                       logger_prefix, table, column, _POSTGREST_RELOAD_HINT)
        return _run(stripped)


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


_LAST_USER_WRITE_ERROR = None
# users.created_at: однажды выяснили, что колонки нет — не дёргаем базу каждым
# запросом профиля (иначе лог забивается PGRST204, а игрок каждый раз ждёт).
_USERS_CREATED_AT_READY = None


def _db_update_user(user_id, **fields):
    """Обновляем только переданные колонки (никаких PGRST204 из-за лишних полей)."""
    global _LAST_USER_WRITE_ERROR
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        return False
    uid = _safe_int(user_id, None)
    if uid is None:
        return False
    _LAST_USER_WRITE_ERROR = None
    # Задача 69: если у игрока остались старые дубли профиля, писать надо только
    # в ТУ строку, которую он видит (максимум коинов). Иначе `update(...).eq(
    # "user_id", uid)` менял все строки сразу: коины начислялись дважды, а
    # покупка списывалась с «мёртвой» строки.
    try:
        row, rows = _read_user_row(uid)
    except Exception as exc:  # noqa: BLE001 — читать не смогли, работаем по старинке
        row, rows = None, []
        logger.warning("Не удалось прочитать профиль user_id=%s перед записью: %s", uid, exc)
    # Строка могла быть сохранена как int, а могла — как текст (старые записи).
    for key in (uid, str(uid)):
        try:
            query = supabase.table("users").update(payload).eq("user_id", key)
            if len(rows) > 1 and row and str(row.get("id")).strip() not in ("", "None"):
                query = query.eq("id", row["id"])
            updated = query.select("user_id").execute()
        except Exception as exc:  # noqa: BLE001
            _LAST_USER_WRITE_ERROR = exc
            logger.error("Не удалось обновить users по user_id=%s (%s): %s",
                         key, ", ".join(sorted(payload)), exc)
            continue
        if updated.data:
            return True
    return False


def _read_user_row(user_id):
    """Профиль игрока целиком: (строка, который видит игрок; все строки).

    Нужен там, где деньги читаются и тут же пишутся: чтобы записать в ТУ ЖЕ
    строку, что показываем игроку (задача 69 — дубли user_id).
    """
    try:
        res = (supabase.table("users")
               .select("id, user_id, coins, inventory")
               .eq("user_id", int(user_id)).execute())
    except Exception:  # noqa: BLE001 — старая база без id/inventory: читаем что есть
        res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    rows = res.data or []
    return _primary_user_row(rows), rows


def _write_primary_coins(user_id, coins: int) -> bool:
    """Записать баланс ровно в ту строку профиля, которую видит игрок.

    Задача 69: `update({"coins": ...}).eq("user_id", uid)` менял ВСЕ дубли
    сразу — деньги начислялись дважды, а списание уходило в «мёртвую» строку.
    Пишем по id живой строки, а по user_id — только если id в базе нет.
    """
    row, _rows = _read_user_row(user_id)
    if row and str(row.get("id")).strip() not in ("", "None"):
        updated = (supabase.table("users").update({"coins": coins})
                   .eq("id", row["id"]).select("user_id").execute())
        return bool(updated.data)
    updated = (supabase.table("users").update({"coins": coins})
               .eq("user_id", int(user_id)).select("user_id").execute())
    return bool(updated.data)


def _backfill_user_created_at(user_id, value) -> bool:
    """Ставим users.created_at старому профилю. Отсутствие колонки — не приговор.

    Если миграция ещё не применена, профиль должен открываться: счётчик
    «дней в игре» просто останется пустым, а в лог один раз уйдёт подсказка,
    какую миграцию выполнить. Раньше обновление пыталось выполняться на каждом
    запросе и забивало лог.
    """
    global _USERS_CREATED_AT_READY
    if _USERS_CREATED_AT_READY is False:
        return False
    if _db_update_user(user_id, created_at=value):
        _USERS_CREATED_AT_READY = True
        return True
    cause = _LAST_USER_WRITE_ERROR
    if cause is not None and _optional_columns_error(cause, ("created_at",)):
        _USERS_CREATED_AT_READY = False
        logger.warning("users.created_at отсутствует — примените %s. %s",
                       _MIGRATION_HINT_BY_COLUMN["created_at"], _POSTGREST_RELOAD_HINT)
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
        # Шанс фиксирован: 90 / X. Никаких множителей из game_settings больше нет —
        # то, что видит игрок в круге, и то, по чему играем, теперь одно число.
        "house_edge": 1.0,
        "chance_factor": 1.0,
        "admin_only": UPGRADE_ADMIN_ONLY,
    }


def _user_payload(row, is_admin, user_id=None):
    """Единый ответ профиля: любые NULL/строки из базы приводим к нормальным типам."""
    # Первый заход в аппку: если отметки ещё нет — это как раз он.
    app_first_seen = _app_first_seen_get(user_id) if user_id is not None else None
    if user_id is not None and app_first_seen is None:
        _app_first_seen_mark(user_id)
        app_first_seen = _APP_FIRST_SEEN_MEMORY.get(int(user_id))
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
    # Задача 49 (уточнение игрока): «Дней с нами» — ТОЛЬКО от первого захода в
    # аппку, дата регистрации в боте не считается. Задача 48 (обнуление 0)
    # остаётся: если отметки нет — ставим её сейчас и больше не двигаем
    # (см. _app_first_seen_mark), поэтому счётчик растёт, а не сбрасывается.
    days_start = app_first_seen
    return {
        "ok": True,
        "coins": _safe_int(row.get("coins"), 0),
        "keys": _safe_int(row.get("keys"), 0),
        "inventory": parse_inventory(row.get("inventory")),
        "last_chest": _safe_int(row.get("last_chest"), 0),
        "is_admin": is_admin,
        # Техработы: приложение показывает экран-заглушку всем, у кого нет доступа.
        "maintenance": MAINTENANCE_MODE or app_closed_now(),
        "maintenance_message": ("" if allowed else _access_denied_message(uid or 0)),
        "admin_only": app_closed_now(),
        "can_play": bool(allowed),
        # Задача 71: админ-панель рисует тумблер по этому полю.
        "maintenance_active": maintenance_now(),
        "maintenance_message": MAINTENANCE_MESSAGE if maintenance_now() else "",
        "created_at": created_text,
        # Задача 43: дни считаем от ПЕРВОГО ЗАХОДА В АППКУ (app_first_seen),
        # а не от регистрации в боте. created_at оставлен для совместимости.
        "days_in_app": _days_since(days_start),
        "app_first_seen": (_iso_from_timestamp(app_first_seen) if app_first_seen else None),
        # Дни — от первого захода в аппку (created_at оставлен для совместимости).
        "days_in_game": _days_since(days_start) if days_start else _days_in_game(created_at),
        "days_since": (_iso_from_timestamp(days_start) if days_start else None),
        # Пассивный доход: клиент считает «в час» по своему инвентарю этими ставками.
        "passive_rates": PASSIVE_INCOME_RATES,
        "passive_max_hours": PASSIVE_INCOME_MAX_HOURS,
        # Пассивка идёт всегда (апка закрыта — копится), ночью по МСК вдвое меньше.
        "passive_night": {"multiplier": PASSIVE_NIGHT_MULTIPLIER,
                          "from": PASSIVE_NIGHT_START_HOUR, "to": PASSIVE_NIGHT_END_HOUR},
        # Задача 42: «сколько уже накопил» прямо в профиле, чтобы карточка
        # показывала накопленное, а не только ставку в час.
        "passive_pending": _passive_pending_public(uid),
        "channel_url": CHANNEL_URL,
        "subscription_bonus": SUBSCRIPTION_BONUS,
        # Доступ и экономика: клиенту нужны те же числа, что играет сервер
        # (шансы апгрейда, лимиты, режим «только админы»).
        "access": {
            "admin_only_app": app_closed_now(),
            "admins": len(ADMIN_IDS),
            "message": ADMIN_ONLY_MESSAGE if app_closed_now() else "",
        },
        "economy": {"upgrade": _upgrade_economy_config()},
    }


# Задача 43: «Дней в игре» = сколько прошло с ПЕРВОГО ЗАХОДА В АППКУ,
# а не с регистрации в боте. Дата регистрации у старых игроков на годы раньше,
# из-за чего плашка показывала неверное число.
APP_FIRST_SEEN_TABLE = "app_first_seen"
_APP_FIRST_SEEN_MEMORY: dict = {}
# Задача 49: какие даты реально лежат в базе. Если таблицы нет (миграция не
# применена), повторяем запись не чаще раза в минуту, а не на каждом запросе.
_APP_FIRST_SEEN_SAVED: set = set()
_APP_FIRST_SEEN_RETRY: dict = {}
_APP_FIRST_SEEN_WARNED = {"once": False}
APP_FIRST_SEEN_RETRY_SECONDS = 60.0


def _app_first_seen_mark(user_id, stamp: float = None) -> bool:
    """Отмечает первый заход в Mini App. True — дата сохранена в базе.

    Задача 49: «Дней с нами» считается ТОЛЬКО от этой даты, поэтому она обязана
    уцелеть после перезапуска сервиса. upsert НЕ перезаписывает существующую
    строку (иначе каждый заход обнулял бы стаж), а если база ответила ошибкой —
    дата остаётся в памяти и попытка повторится на следующем запросе профиля.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in _APP_FIRST_SEEN_MEMORY and uid in _APP_FIRST_SEEN_SAVED:
        return True
    if uid in _APP_FIRST_SEEN_MEMORY and uid not in _APP_FIRST_SEEN_SAVED:
        # Дата уже есть в памяти, но в базе её нет — пробуем дописать, но не
        # чаще раза в минуту (без миграции иначе это запрос на каждый заход).
        if time.time() - float(_APP_FIRST_SEEN_RETRY.get(uid) or 0) < APP_FIRST_SEEN_RETRY_SECONDS:
            return False
        stamp = float(_APP_FIRST_SEEN_MEMORY[uid])
        _APP_FIRST_SEEN_RETRY[uid] = time.time()
        try:
            _insert_tolerant(APP_FIRST_SEEN_TABLE,
                             {"user_id": uid, "first_seen": _iso_from_timestamp(stamp)})
            _APP_FIRST_SEEN_SAVED.add(uid)
            return True
        except Exception:  # noqa: BLE001
            return False
    stamp = float(stamp if stamp is not None else time.time())
    try:
        res = (supabase.table(APP_FIRST_SEEN_TABLE).select("first_seen")
               .eq("user_id", uid).limit(1).execute())
        if res.data:
            parsed = _parse_timestamp(res.data[0].get("first_seen"))
            _APP_FIRST_SEEN_MEMORY[uid] = float(parsed or stamp)
            _APP_FIRST_SEEN_SAVED.add(uid)
            return True
    except Exception:  # noqa: BLE001 — читаем память, ниже попробуем записать
        pass
    _APP_FIRST_SEEN_MEMORY[uid] = stamp
    try:
        # Именно insert: на существующей строке upsert перезаписал бы дату
        # первого захода, и стаж обнулился бы на каждом заходе.
        _insert_tolerant(APP_FIRST_SEEN_TABLE,
                         {"user_id": uid, "first_seen": _iso_from_timestamp(stamp)})
        _APP_FIRST_SEEN_SAVED.add(uid)
        _APP_FIRST_SEEN_RETRY.pop(uid, None)
        return True
    except Exception as exc:  # noqa: BLE001 — нет миграции? профиль не падает
        _APP_FIRST_SEEN_RETRY[uid] = time.time()
        if not _APP_FIRST_SEEN_WARNED["once"]:
            _APP_FIRST_SEEN_WARNED["once"] = True
            logger.warning("app_first_seen недоступна (%s): %s — дата держится в памяти, "
                           "примените supabase/migrations/20261027000000_app_first_seen.sql",
                           APP_FIRST_SEEN_TABLE, exc)
        return False


def _app_first_seen_get(user_id):
    """Когда игрок впервые открыл аппку (timestamp) или None."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    try:
        res = (supabase.table(APP_FIRST_SEEN_TABLE).select("first_seen")
               .eq("user_id", uid).limit(1).execute())
        if res.data:
            parsed = _parse_timestamp(res.data[0].get("first_seen"))
            if parsed:
                _APP_FIRST_SEEN_MEMORY.setdefault(uid, float(parsed))
                return float(parsed)
    except Exception:  # noqa: BLE001
        pass
    return _APP_FIRST_SEEN_MEMORY.get(uid)


def _earliest_stamp(*stamps):
    """Самая ранняя отметка времени из переданных (None и мусор пропускаем)."""
    values = []
    for stamp in stamps:
        try:
            if stamp is None:
                continue
            value = float(stamp)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    return min(values) if values else None


def _days_since(stamp) -> int:
    """Сколько полных дней прошло с момента stamp."""
    if not stamp:
        return 0
    try:
        return max(0, int((time.time() - float(stamp)) // 86400))
    except (TypeError, ValueError):
        return 0


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
        _insert_tolerant(IDEMPOTENCY_TABLE, {
            "key": key, "user_id": int(user_id), "endpoint": endpoint,
            "response": json.dumps(payload, ensure_ascii=False),
            "created_at": _now_iso(),
        }, upsert=True, on_conflict="key", logger_prefix="IDEMPOTENCY: ")
    except Exception as exc:  # noqa: BLE001 — не роняем уже совершённую операцию
        logger.warning("Не удалось сохранить idempotency-ключ %s: %s", key, exc)


def _enforce_bet_limit(user_id, total_bet):
    """Строгая серверная проверка общей ставки: > MAX_BET запрещено всем, кроме админов."""
    if _is_admin_user(user_id):
        return
    if int(total_bet) > MAX_BET:
        raise HTTPException(status_code=400, detail=f"Максимальная ставка {MAX_BET} коинов")


# === АРЕНА ===
# Две команды командной арены. Цвета — те же, что у красной/синей кнопки в UI.
ARENA_TEAMS = {
    "red": {"name": "Красные", "color": "#ff3b30"},
    "blue": {"name": "Синие", "color": "#007aff"},
}


# Задача 31: в одном раунде живут ДВЕ независимые арены.
#   • normal — «сектор на каждого игрока» (полосы + мяч);
#   • team   — «красные против синих» (круг со стрелкой).
# Раньше режим был один на весь сервер и переключался только между раундами,
# поэтому нельзя было посмотреть вторую арену, пока идёт бой. Теперь оба котла
# считаются одновременно, а переключатель в Mini App — это только выбор того,
# что рисовать: он работает в любой момент и ничего не блокирует.
ARENA_VIEWS = ("normal", "team")
ARENA_MAX_PLAYERS = 20          # на каждый котёл отдельно
# Задача 56: минимальная ставка. Ставка в 1 коин за фаворита забирала долю
# банка, накопленного ботами, — это выглядело как «весь банк за 1 коин».
ARENA_MIN_BET = 100
# Задача 43: командная арена — маленький котёл. Максимум 2–3 бота на раунд,
# ставки 250…500, без «китов»: там играют командами, а не гоняют ботов.
TEAM_BOT_COUNT_MIN = 2
TEAM_BOT_COUNT_MAX = 3
TEAM_BOT_BET_MIN = 250
TEAM_BOT_BET_MAX = 500


def _player_arena(player) -> str:
    """К какому котлу относится запись игрока (старые записи — обычная арена)."""
    return "team" if player.get("arena") == "team" else "normal"


def _arena_players(arena: str) -> list:
    arena = "team" if arena == "team" else "normal"
    return list(_arena_state(arena)["players"])


def _arena_bank(arena: str) -> int:
    return sum(max(0, int(player.get("bet", 0))) for player in _arena_players(arena))


def _new_arena_state(mode: str = "both", arena: str = "normal") -> dict:
    # mode оставлен для совместимости со старым клиентом; по умолчанию оба котла.
    return {
        "status": "waiting", "timer": 15, "bank": 0, "players": [], "winner": None,
        "last_update": time.time(), "last_bot_added": time.time(),
        "target_bots": _random_arena_bot_count(arena), "is_paid": False,
        "payout_attempt_at": 0,
        # Задача 47: свой слот раунда уже на старте — раньше оба котла
        # получали ОДИН round_id (одна и та же секунда), и клиент мог
        # перепутать, чей это раунд.
        "round_id": str(int(time.time()) * 10 + (0 if arena == "normal" else 7)),
        "whales_added": 0,
        "roll_data": None, "rolling_started_at": None, "whale_decided": False,
        "whale_join": False, "whale_trigger_at": None, "whale_arena": "normal",
        "mode": "both",
        "winner_team": None, "payouts": [], "paid_users": [],
        # Победитель этого котла. Оставлено словарём, чтобы старый клиент,
        # ждавший arena_winners, не сломался (см. _hockey_state_response).
        "arena_winners": {"normal": None, "team": None},
    }


# Задача 42: у КАЖДОЙ арены свой раунд и своё состояние. Раньше это был один
# общий game_state: одиночная и командная стартовали и заканчивались вместе,
# поэтому командная «шла столько же, сколько одиночная», хотя по правилам это
# отдельная игра со своим таймингом (разгон 3 с + торможение 5 с) и своим
# розыгрышем. Теперь их можно смотреть и ставить в любую независимо, а раунды
# не совпадают по времени.
ARENAS: dict = {"normal": _new_arena_state(arena="normal"), "team": _new_arena_state(arena="team")}


def _arena_state(arena: str = "normal") -> dict:
    return ARENAS.get("team" if arena == "team" else "normal", ARENAS["normal"])


class _ArenaStateProxy:
    """Чтение/запись полей текущего котла.

    Оставлен, чтобы вспомогательные функции (боты, выплаты, финиш) продолжали
    работать с прежним синтаксисом `game_state["..."]`, но каждый котёл при
    этом имел своё состояние.
    """

    def __init__(self, arena="normal"):
        self.arena = arena

    def _state(self):
        return _arena_state(self.arena)

    def __getitem__(self, key):
        return self._state()[key]

    def __setitem__(self, key, value):
        self._state()[key] = value

    def get(self, key, default=None):
        return self._state().get(key, default)

    def setdefault(self, key, default=None):
        return self._state().setdefault(key, default)

    def copy(self):
        return dict(self._state())

    def update(self, **kwargs):
        self._state().update(kwargs)


game_state = _ArenaStateProxy("normal")
history = []


def _arena_round_status(arena: str) -> str:
    return _arena_state(arena).get("status", "waiting")


def _arena_round_id(arena: str) -> str:
    return str(_arena_state(arena).get("round_id"))


HOCKEY_ROLL_DURATION = 9.0
HOCKEY_WAIT_DURATION = 15.0
# Задача 34: после броска раунд висит в «finished» не 5, а 9 секунд. Стрелка в
# командной арене тормозит ровно как в апгрейде (5 с), и только потом выезжает
# плашка победителя — раньше новый раунд начинался раньше, чем заканчивался
# финиш, и анимация обрывалась на середине.
HOCKEY_FINISH_HOLD = 9.0
HOCKEY_ROUND_PERIOD = HOCKEY_WAIT_DURATION + HOCKEY_ROLL_DURATION + HOCKEY_FINISH_HOLD
# Задача 42: у КОМАНДНОЙ арены свой тайминг — это отдельная игра, а не «второй
# вид» одиночной. Бросок стрелки синхронизирован с клиентом (arenaLandPointer
# в miniapp/app.js); задача 44 сократила его с 8 с до 5.5 с.
HOCKEY_TEAM_WAIT_DURATION = 15.0     # набор ставок в командной — свой, независимый
# Задача 44: бросок сокращён до 5.5 с (2 с разгон + 3.5 с торможение) — ровно
# столько же длится анимация стрелки в miniapp/app.js (ARENA_SPIN_UP_MS /
# ARENA_SPIN_DOWN_MS). Сервер и клиент обязаны совпадать, иначе плашка
# победителя выезжает до остановки стрелки.
HOCKEY_TEAM_SPIN_UP = 2.0
HOCKEY_TEAM_SPIN_DOWN = 3.5
HOCKEY_TEAM_ROLL_DURATION = HOCKEY_TEAM_SPIN_UP + HOCKEY_TEAM_SPIN_DOWN
HOCKEY_TEAM_FINISH_HOLD = 6.0


def _round_roll_duration(arena: str) -> float:
    """Сколько крутится конкретная арена: одиночная мяч 9 с, командная стрелка 5.5 с."""
    return HOCKEY_TEAM_ROLL_DURATION if arena == "team" else HOCKEY_ROLL_DURATION


def _round_finish_hold(arena: str) -> float:
    return HOCKEY_TEAM_FINISH_HOLD if arena == "team" else HOCKEY_FINISH_HOLD
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


def _prepare_hockey_roll(now, arena: str = "normal"):
    """Траектория/угол броска конкретного котла (своя у каждой арены)."""
    state = _arena_state(arena)
    # Задача 42: у каждой арены свой слот раунда, поэтому раунды не совпадают
    # по времени, а round_id не пересекаются между котлами.
    period = HOCKEY_WAIT_DURATION + _round_roll_duration(arena) + _round_finish_hold(arena)
    slot_shift = 0 if arena == "normal" else 7
    round_slot = int(now // period) * 10 + slot_shift
    state["round_id"] = str(round_slot)
    round_rng = random.Random(round_slot)
    start_angle = round_rng.uniform(0.0, 2 * math.pi)
    # Скорость подобрана под ease-out: суммарный путь примерно как у прежней модели.
    speed = 18.0
    start_x, start_y = 50.0, 50.0
    path = _simulate_hockey_roll(start_angle, speed, start_x, start_y)
    state["winner"] = None
    state["winner_team"] = None
    state["rolling_started_at"] = now
    state["roll_data"] = {
        "start_angle": start_angle,
        "speed": speed,
        "start_x": start_x,
        "start_y": start_y,
        "final_x": path[-1][0],
        "final_y": path[-1][1],
        "path": path,
    }


def _update_hockey_percentages(arena: str = None):
    """Проценты внутри котла. Без аргумента — пересчитать оба (они независимы)."""
    targets = [arena] if arena in ARENA_VIEWS else list(ARENA_VIEWS)
    for target in targets:
        state = _arena_state(target)
        state["bank"] = sum(max(0, int(p.get("bet", 0))) for p in state["players"])
    for arena in targets:
        pool = _arena_players(arena)
        total = sum(max(0, int(player.get("bet", 0))) for player in pool)
        for player in pool:
            player["percent"] = (max(0, int(player.get("bet", 0))) / total * 100) if total else 0


def _unique_hockey_color(palette, arena="normal"):
    used = {player.get("color") for player in _arena_players(arena)}
    available = [color for color in palette if color not in used]
    if available:
        return random.choice(available)
    return f"hsl({random.randint(0, 360)}, 80%, 50%)"


def _add_hockey_bot(is_whale=False, arena="normal"):
    """Бот в конкретный котёл: у каждой арены свои участники."""
    arena = "team" if arena == "team" else "normal"
    if len(_arena_players(arena)) >= ARENA_MAX_PLAYERS:
        return False
    if arena == "team":
        # Задача 43: в командной арене не больше TEAM_BOT_COUNT_MAX ботов и
        # никаких китов — проверка прямо здесь, чтобы лимит держался при любом
        # вызове (боты, ручные тесты, старый код).
        if is_whale:
            return False
        team_bots = sum(1 for player in _arena_players("team") if player.get("is_bot"))
        if team_bots >= TEAM_BOT_COUNT_MAX:
            return False

    if is_whale:
        name = "Лудик" + str(random.randint(1, 99))
        bet = random.randint(500, 2000)
        color = _unique_hockey_color(WHALE_COLORS, arena)
    elif arena == "team":
        # Задача 43: в командной арене ставки ботов — 250…500, без «мелочи»
        # по 10 коинов: иначе банк выглядел пустым и делить было нечего.
        name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
        bet = random.randint(TEAM_BOT_BET_MIN, TEAM_BOT_BET_MAX)
        color = _unique_hockey_color(BOT_COLORS, arena)
    else:
        name = random.choice(BOT_NAMES) + str(random.randint(1, 99))
        bet = random.randint(10, 100)
        color = _unique_hockey_color(BOT_COLORS, arena)

    team = None
    if arena == "team":
        # Команды держим примерно равными: иначе боты сбиваются в одну сторону
        # и круг командной арены выглядит как один сплошной сектор.
        counts = {"red": 0, "blue": 0}
        for mate in _arena_players("team"):
            if mate.get("team") in counts:
                counts[mate["team"]] += 1
        team = secure_choice([side for side in ("red", "blue") if counts[side] == min(counts.values())])
    _arena_state(arena)["players"].append({
        "id": f"bot_{random.randint(10000, 99999)}", "name": name, "avatar": "",
        "bet": bet, "color": ARENA_TEAMS[team]["color"] if team else color,
        "is_bot": True, "team": team, "arena": arena,
    })
    _update_hockey_percentages(arena)
    return True


def _maybe_add_hockey_bots(now, time_left):
    if time_left <= 0:
        return

    # Боты набегают в ОБА котла одновременно (задача 31): при переключении
    # арена не должна быть пустой. Раньше здесь стоял break — он наполнял
    # только первую арену, и командная к середине раунда оставалась пустой.
    for arena in ARENA_VIEWS:
        state = _arena_state(arena)
        pool = _arena_players(arena)
        current_bots = sum(1 for p in pool if p.get("is_bot"))
        # В командной арене ботов ровно 2–3 на раунд (задача 43). Игроков это
        # НЕ ограничивает: лимит только на ботов, иначе не пустили бы живых.
        limit = ARENA_MAX_PLAYERS
        if (current_bots < state["target_bots"] and len(pool) < limit
                and now - state["last_bot_added"] >= random.uniform(0.6, 1.5)):
            if _add_hockey_bot(arena=arena):
                state["last_bot_added"] = now

        # Кит приходит только в свой котёл и по своим таймерам.
        # В командную арену китов НЕ пускаем: там фиксированные ставки 250–500.
        if (arena == "normal" and state.get("whale_join") and not state.get("whales_added")
                and state.get("whale_trigger_at") is not None
                and time_left <= state["whale_trigger_at"]
                and len(pool) < ARENA_MAX_PLAYERS):
            if _add_hockey_bot(is_whale=True, arena=arena):
                state["whales_added"] = 1


def _arena_mode() -> str:
    """Совместимость со старым клиентом: теперь всегда работают оба котла."""
    return "both"


def _hockey_teams() -> list:
    """Сводка по командам командного котла: сумма ставок и доля круга."""
    totals = {"red": 0, "blue": 0}
    for player in _arena_players("team"):
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


def _hockey_sectors(arena: str = "normal") -> list:
    """Сектора котла в том порядке, в котором по ним ходит бросок.

    normal — по игроку на сектор, team — два сектора по ставкам команд.
    Порядок один и тот же на сервере и на клиенте, поэтому финальная точка
    траектории всегда попадает в тот сектор, который подсвечен победителем.
    """
    if arena == "team":
        return _hockey_teams()
    return [dict(player) for player in _arena_players("normal")]


def _hockey_winner_at_x(x, arena: str = "normal"):
    """Resolve a final ball x-coordinate to the bet-weighted sector of one pool."""
    sectors = _hockey_sectors(arena)
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


def _arena_payout_plan(arena: str) -> list:
    """Кому и сколько платим в ОДНОМ котле. Считается один раз на раунд котла.

    Обычная арена: банк котла целиком одному победителю (как было).
    Командная: банк котла делится между реальными игроками победившей команды
    пропорционально их ставкам. Доля ботов не выплачивается — это дом-эдж.
    Деньги котлов не смешиваются, поэтому «поставить в обе» не даёт арбитража.
    Задача 42: котлы живут разными раундами, поэтому и план у каждого свой.
    """
    state = _arena_state(arena)
    cached = state.get("payouts") or []
    if cached:
        return cached
    plan = []
    winner = (state.get("arena_winners") or {}).get(arena) or state.get("winner")
    bank = _arena_bank(arena)

    if arena == "team":
        if winner and bank > 0:
            winner_team = winner.get("team") or winner.get("id")
            team_players = [p for p in _arena_players("team") if p.get("team") == winner_team]
            # Задача 56: командный котёл — честный пари-мютюэль.
            #
            # Раньше ставки ботов выкидывались из знаменателя: игрок ставил
            # 1 коин за синих, где бот поставил 868, получал ВЕСЬ банк (1169)
            # при шансе 74% — мат.ожидание 869 коинов за ставку в 1 коин.
            # Это была дыра в экономике, а не «процент».
            #
            # Теперь банк делится на ВСЕ ставки победившей команды — вместе с
            # ботами: доля = bank * bet / team_total. Ставка 1 из 869 даёт
            # 1169 * 1/869 ≈ 1.35 коина (это и есть «ставка + процент»).
            # Мат.ожидание при этом ровно равно ставке — как в пари-мютюэле:
            #   шанс команды = team_total / bank, выплата = bank / team_total.
            team_total = sum(max(0, int(player.get("bet", 0))) for player in team_players)
            humans = []
            for player in team_players:
                if player.get("is_bot"):
                    continue
                try:
                    numeric_id = int(player["id"])
                except (TypeError, ValueError):
                    continue
                humans.append((numeric_id, max(0, int(player.get("bet", 0))), player.get("name")))
            if team_total > 0 and humans:
                # Остаток от округления (1-2 коина) раздаём по крупнейшей
                # дробной части — иначе сумма выплат была бы меньше банка.
                raw = [(uid, bank * bet / team_total, bet, name)
                       for uid, bet, name in humans]
                amounts = {uid: int(value) for uid, value, _, _ in raw}
                leftover = bank - sum(amounts.values())
                # Задача 56: остаток округления достаётся только тем, у кого
                # выплата уже есть. Иначе игрок со ставкой 0 или «0 из банка»
                # получал 1 коин из воздуха.
                eligible = sorted((item for item in raw if amounts.get(item[0], 0) > 0),
                                  key=lambda item: -(item[1] - int(item[1])))
                for uid, value, _bet, _name in eligible:
                    if leftover <= 0:
                        break
                    amounts[uid] += 1
                    leftover -= 1
                for uid, _value, _bet, name in raw:
                    if amounts.get(uid, 0) > 0:
                        plan.append({"user_id": uid, "amount": amounts[uid],
                                     "name": name, "arena": "team"})
    elif winner and not winner.get("is_bot") and bank > 0:
        try:
            numeric_id = int(winner["id"])
        except (TypeError, ValueError):
            numeric_id = None
        if numeric_id is not None:
            plan.append({"user_id": numeric_id, "amount": int(bank),
                         "name": winner.get("name"), "arena": "normal"})

    state["payouts"] = plan
    return plan


def _hockey_payout_plan() -> list:
    """Совместимость: план по обоим котлам (каждый раунд свой)."""
    return _arena_payout_plan("normal") + _arena_payout_plan("team")


ARENA_PAYOUTS_TABLE = "arena_payouts"
ARENA_PAYOUT_RPC = "arena_payout_apply"
PAYOUT_MAX_ATTEMPTS = 60          # ~3 минуты повторов; деньги при этом не теряются
PAYOUT_RETRY_SECONDS = 3.0
# Что реально доступно в базе: журнал выплат и атомарная функция. Определяем
# один раз лениво, чтобы игра работала и до применения миграции.
_ARENA_BACKEND = {"checked": False, "ledger": False, "rpc": False}


def _arena_payout_backend() -> dict:
    global _ARENA_BACKEND
    if _ARENA_BACKEND["checked"]:
        return _ARENA_BACKEND
    ledger = False
    rpc_ok = False
    try:
        supabase.table(ARENA_PAYOUTS_TABLE).select("arena").limit(1).execute()
        ledger = True
    except Exception as exc:  # noqa: BLE001 — таблицы может ещё не быть
        logger.info("Арена: журнал %s недоступен (%s) — выплаты пойдут прямым путём",
                    ARENA_PAYOUTS_TABLE, exc)
    if ledger:
        try:
            # amount=0 — безопасная проба: функция сразу возвращает false и
            # ничего не пишет.
            supabase.rpc(ARENA_PAYOUT_RPC, {
                "p_arena": "_probe", "p_round_id": "_probe",
                "p_user_id": 0, "p_amount": 0}).execute()
            rpc_ok = True
        except Exception as exc:  # noqa: BLE001
            logger.info("Арена: функция %s недоступна (%s) — атомарность через журнал",
                        ARENA_PAYOUT_RPC, exc)
    _ARENA_BACKEND = {"checked": True, "ledger": ledger, "rpc": rpc_ok}
    return _ARENA_BACKEND


def _ledger_row_status(arena: str, round_id: str, user_id: int):
    """Статус строки журнала: 'pending' | 'paid' | None (строки нет) | '?' (нет таблицы)."""
    try:
        rows = (supabase.table(ARENA_PAYOUTS_TABLE)
                .select("status")
                .eq("arena", arena).eq("round_id", round_id).eq("user_id", user_id)
                .limit(1).execute().data) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("arena_payouts: чтение не удалось: %s", exc)
        return "?"
    return (rows[0].get("status") or "pending") if rows else None


def _ledger_add_pending(entry: dict) -> bool:
    """Записать в журнал «должны столько» ДО начисления денег.

    insert (а не upsert) — существующую строку нельзя сбросить из paid в
    pending, иначе после рестарта можно было бы заплатить дважды.
    """
    payload = {
        "arena": str(entry["arena"]),
        "round_id": str(entry["round_id"]),
        "user_id": int(entry["user_id"]),
        "amount": int(entry["amount"]),
        "status": "pending",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        supabase.table(ARENA_PAYOUTS_TABLE).insert(payload).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — дубль ключа тоже сюда попадает
        logger.debug("arena_payouts: строка %s/%s/%s не создана: %s",
                     payload["arena"], payload["round_id"], payload["user_id"], exc)
        return _ledger_row_status(payload["arena"], payload["round_id"],
                                  payload["user_id"]) is not None


def _ledger_mark_paid(entry: dict) -> None:
    try:
        (supabase.table(ARENA_PAYOUTS_TABLE)
         .update({"status": "paid", "paid_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
         .eq("arena", str(entry["arena"])).eq("round_id", str(entry["round_id"]))
         .eq("user_id", int(entry["user_id"])).execute())
    except Exception as exc:  # noqa: BLE001
        logger.debug("arena_payouts: статус не обновился: %s", exc)


def _credit_arena_payout(entry: dict) -> str:
    """Начислить один выигрыш. Возвращает 'paid' | 'duplicate' | 'error'.

    'duplicate' — деньги уже начислены этим же раундом (повторная попытка или
    рестарт), второй раз платить нельзя.
    """
    # Задача 74: выигрыш в арене считается ТОЛЬКО здесь — это единственная точка,
    # где деньги реально начисляются (включая доплаты после рестарта), поэтому
    # в профиле не бывает «двух побед» на одну выплату. Возврат ставки (refund)
    # победой не считается: это свои же деньги.
    if str(entry.get("kind") or "") != "refund":
        stats_note(entry["user_id"], coins_earned=int(entry["amount"]),
                   won_arena=int(entry["amount"]), best_arena_win=int(entry["amount"]))
        ledger_note(entry["user_id"], "win", int(entry["amount"]), "🏒 Арена")
    elif int(entry.get("amount") or 0) > 0:
        # Возврат ставки после рестарта — не выигрыш, но деньги игрок получил.
        ledger_note(entry["user_id"], "claim", int(entry["amount"]), "↩️ Возврат ставки")
    arena = str(entry["arena"])
    round_id = str(entry["round_id"])
    uid = int(entry["user_id"])
    amount = int(entry["amount"])
    backend = _arena_payout_backend()

    if backend["ledger"]:
        status = _ledger_row_status(arena, round_id, uid)
        if status == "paid":
            return "duplicate"
        if status is None:
            if not _ledger_add_pending(entry):
                raise RuntimeError("не удалось записать выплату в журнал arena_payouts")
        if backend["rpc"]:
            # Одна транзакция «отметить paid + начислить монеты».
            applied = supabase.rpc(ARENA_PAYOUT_RPC, {
                "p_arena": arena, "p_round_id": round_id,
                "p_user_id": uid, "p_amount": amount}).execute().data
            if applied:
                return "paid"
            # false: строку уже перевели в paid ранее → задвоения нет.
            return "duplicate"
        # Журнал есть, функции нет — начисляем и помечаем вручную.
        _direct_arena_credit(uid, amount)
        _ledger_mark_paid(entry)
        return "paid"

    _direct_arena_credit(uid, amount)
    return "paid"


def _direct_arena_credit(user_id: int, amount: int) -> None:
    """Старый путь выплаты: прочитать баланс, прибавить, записать."""
    res = supabase.table("users").select("coins").eq("user_id", user_id).execute()
    if not res.data:
        raise RuntimeError(f"Победитель Арены user_id={user_id} не найден в users")
    _row = _primary_user_row(res.data)
    if _row is None:
        raise RuntimeError(f"Победитель Арены user_id={user_id} не найден в users")
    new_coins = _safe_int(_row.get("coins"), 0) + int(amount)
    if not _write_primary_coins(user_id, new_coins):
        raise RuntimeError(f"Supabase не подтвердил выплату user_id={user_id}")


ARENA_ROUNDS_TABLE = "arena_rounds"
ARENA_SNAPSHOT_SECONDS = 2.0
_ARENA_SNAPSHOT = {"checked": False, "ok": False, "at": 0.0}


def _arena_rounds_available() -> bool:
    """Есть ли в базе таблица снимков раундов (миграция 20261127000000)."""
    if _ARENA_SNAPSHOT["checked"]:
        return _ARENA_SNAPSHOT["ok"]
    try:
        supabase.table(ARENA_ROUNDS_TABLE).select("arena").limit(1).execute()
        ok = True
    except Exception as exc:  # noqa: BLE001 — без таблицы игра работает как раньше
        logger.info("Арена: таблица %s недоступна (%s) — раунды только в памяти",
                    ARENA_ROUNDS_TABLE, exc)
        ok = False
    _ARENA_SNAPSHOT.update({"checked": True, "ok": ok})
    return ok


def _arena_snapshot_payload(arena: str) -> dict:
    state = _arena_state(arena)
    # Траекторию мяча (сотни точек) в снимок НЕ кладём: для доигрывания нужен
    # только final_x/final_y, а jsonb на 2 записи в секунду раздувать незачем.
    roll_data = state.get("roll_data") or None
    if roll_data:
        roll_data = {k: v for k, v in roll_data.items() if k != "path"}
    return {
        "status": state.get("status"),
        "round_id": str(state.get("round_id")),
        "bank": int(state.get("bank") or 0),
        "winner": state.get("winner"),
        "winner_team": state.get("winner_team"),
        "roll_data": roll_data,
        "rolling_started_at": state.get("rolling_started_at"),
        "last_update": state.get("last_update"),
        "is_paid": bool(state.get("is_paid")),
        "payouts": state.get("payouts") or [],
        "paid_users": state.get("paid_users") or [],
        "players": [dict(player) for player in _arena_players(arena)],
        "saved_at": time.time(),
    }


def _save_arena_snapshot(arena: str) -> None:
    """Сохранить снимок раунда. Ошибки не мешают игре."""
    if not _arena_rounds_available():
        return
    payload = _arena_snapshot_payload(arena)
    try:
        supabase.table(ARENA_ROUNDS_TABLE).upsert({
            "arena": arena, "round_id": payload["round_id"], "state": payload,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, on_conflict="arena").execute()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Арена %s: снимок не сохранился: %s", arena, exc)


def _save_arena_snapshots(force: bool = False) -> None:
    now = time.time()
    if not force and now - float(_ARENA_SNAPSHOT.get("at") or 0) < ARENA_SNAPSHOT_SECONDS:
        return
    _ARENA_SNAPSHOT["at"] = now
    for arena in ARENA_VIEWS:
        _save_arena_snapshot(arena)


def _restore_arena_rounds() -> dict:
    """Поднять раунды из базы после перезапуска сервиса.

    Возвращает {'resumed': [...], 'refunded': [...]} — что доиграли и где
    пришлось вернуть ставки (исход неизвестен, деньги игрока не должны сгорать).
    """
    result = {"resumed": [], "refunded": []}
    if not _arena_rounds_available():
        return result
    try:
        rows = supabase.table(ARENA_ROUNDS_TABLE).select("*").execute().data or []
    except Exception as exc:  # noqa: BLE001
        logger.info("Арена: снимки раундов не прочитались: %s", exc)
        return result

    for row in rows:
        arena = str(row.get("arena") or "")
        if arena not in ARENA_VIEWS:
            continue
        saved = row.get("state") or {}
        if not isinstance(saved, dict):
            continue
        restored = _new_arena_state(arena=arena)
        restored.update({key: value for key, value in saved.items()
                         if key in restored and key not in {"status"}})
        restored["status"] = "waiting"
        restored["timer"] = 0
        restored["payout_status"] = "none"
        restored["payout_running"] = False
        restored["payout_attempts"] = 0
        restored["payout_attempt_at"] = 0
        restored["last_update"] = time.time()
        restored["last_bot_added"] = time.time()
        ARENAS[arena] = restored

        players = restored.get("players") or []
        humans = [p for p in players if not p.get("is_bot")]
        if not humans:
            continue                       # ставок людей не было — нечего спасать
        if saved.get("is_paid"):
            continue                       # раунд уже был закрыт по деньгам
        if saved.get("status") == "rolling" and saved.get("roll_data"):
            # Исход уже определён сервером: доигрываем и платим по-настоящему.
            restored["status"] = "rolling"
            restored["last_update"] = float(saved.get("last_update") or (time.time() - 60))
            restored["rolling_started_at"] = saved.get("rolling_started_at")
            result["resumed"].append({"arena": arena, "round_id": saved.get("round_id"),
                                      "players": len(humans)})
            logger.info("Арена %s: раунд %s доигрывается после рестарта (%s игроков)",
                        arena, saved.get("round_id"), len(humans))
        else:
            # Ставки собраны, но победитель не определён (рестарт во время
            # набора или финиша). Возвращаем ставки: деньги игрока не сгорают.
            for player in humans:
                try:
                    amount = int(player.get("bet") or 0)
                    uid = int(player.get("id"))
                except (TypeError, ValueError):
                    continue
                if amount <= 0:
                    continue
                refund = {"arena": arena, "round_id": str(saved.get("round_id") or ""),
                          "user_id": uid, "amount": amount, "kind": "refund",
                          "status": "pending"}
                try:
                    _credit_arena_payout(refund)
                    result["refunded"].append({"arena": arena, "user_id": uid, "amount": amount})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Арена %s: возврат ставки %s не прошёл: %s", arena, uid, exc)
            restored["players"] = []
            restored["bank"] = 0
            restored["payouts"] = []
            restored["paid_users"] = []
            restored["is_paid"] = True
            restored["payout_status"] = "refunded"
            if result["refunded"]:
                logger.info("Арена %s: после рестарта возвращены ставки (%s)",
                            arena, len(result["refunded"]))
    _save_arena_snapshots(force=True)
    return result


# Очередь выплат: долг, который ещё не подтверждён базой. Живёт в памяти, но
# каждая строка ДО начисления попадает в журнал arena_payouts, поэтому даже
# рестарт сервиса не теряет деньги (см. _resume_pending_payouts).
PENDING_PAYOUTS: dict = {}
PENDING_PAYOUTS_LOCK = threading.Lock()
_DRAIN_LOCK = threading.Lock()
_LAST_PAYOUT_DRAIN = {"at": 0.0}
PENDING_PAYOUTS_MAX_ATTEMPTS = 1000     # ~50 минут повторов; журнал в базе остаётся


def _pending_key(arena: str, round_id: str, user_id) -> str:
    return f"{arena}:{round_id}:{user_id}"


def _queue_payout_entries(arena: str, round_id: str, plan, paid_keys) -> None:
    """Положить неподтверждённые выплаты раунда в очередь на повтор."""
    with PENDING_PAYOUTS_LOCK:
        for entry in plan:
            key = _pending_key(arena, round_id, entry["user_id"])
            if key in paid_keys or key in PENDING_PAYOUTS:
                continue
            PENDING_PAYOUTS[key] = {
                "arena": arena, "round_id": round_id,
                "user_id": int(entry["user_id"]), "amount": int(entry["amount"]),
                "attempts": 0, "last_error": "",
            }


def _sync_arena_paid_flag(arena: str) -> None:
    """Раунд закрыт только когда в очереди не осталось ни одной его выплаты."""
    state = _arena_state(arena)
    round_id = str(state.get("round_id"))
    with PENDING_PAYOUTS_LOCK:
        left = [key for key in PENDING_PAYOUTS
                if key.startswith(f"{arena}:{round_id}:")]
    if left:
        state["payout_status"] = "pending"
        return
    if not state.get("is_paid"):
        state["is_paid"] = True
        state["payout_status"] = "paid"
        state["payout_error"] = ""
        # Задача 47: закрытый раунд фиксируем в базе — после рестарта он уже
        # не будет ни доигран, ни возвращён повторно.
        _save_arena_snapshot(arena)
        return
    _save_arena_snapshot(arena)


def _drain_pending_payouts() -> int:
    """Повторить все долги по аренам. Возвращает число закрытых выплат.

    Дренаж всегда один за раз (_DRAIN_LOCK): иначе две параллельные попытки
    могли бы начислить одно и то же дважды на «прямом» пути без журнала.
    """
    if not _DRAIN_LOCK.acquire(blocking=False):
        return 0
    try:
        return _drain_pending_payouts_locked()
    finally:
        _DRAIN_LOCK.release()


def _drain_pending_payouts_locked() -> int:
    with PENDING_PAYOUTS_LOCK:
        entries = list(PENDING_PAYOUTS.items())
    closed = 0
    for key, entry in entries:
        try:
            result = _credit_arena_payout(entry)
        except Exception as exc:  # noqa: BLE001 — долг остаётся в очереди
            with PENDING_PAYOUTS_LOCK:
                live = PENDING_PAYOUTS.get(key)
                if live is None:
                    continue
                live["attempts"] += 1
                live["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                if live["attempts"] >= PENDING_PAYOUTS_MAX_ATTEMPTS:
                    PENDING_PAYOUTS.pop(key, None)
                    logger.error("Арена: выплата %s не прошла после %s попыток: %s",
                                 key, live["attempts"], exc)
            continue
        if result in ("paid", "duplicate"):
            with PENDING_PAYOUTS_LOCK:
                PENDING_PAYOUTS.pop(key, None)
            closed += 1
            _sync_arena_paid_flag(entry["arena"])
    return closed


def _payout_arena_winner(arena: str) -> bool:
    """Выплатить банк конкретного котла. True — выплата закрыта (или платить некому).

    Задача 47: раньше выплата была ОДНОЙ попыткой прямо в запросе, и любая
    осечка базы («сбрасывается и баланс не отдаёт») означала потерянные деньги.
    Теперь:
      * каждая строка плана пишется в журнал arena_payouts ДО начисления;
      * начисление атомарно (функция arena_payout_apply) и идемпотентно —
        повтор не может задвоить выигрыш;
      * неподтверждённые строки лежат в очереди PENDING_PAYOUTS и повторяются
        каждые 3 с, в том числе после рестарта сервиса;
      * новый раунд не начинается, пока старый не закрыт по деньгам.
    """
    state = _arena_state(arena)
    if state["is_paid"]:
        _sync_arena_paid_flag(arena)   # очередь могла ещё не опустеть
        return state["is_paid"]
    plan = _arena_payout_plan(arena)
    round_id = str(state.get("round_id"))
    if not plan:
        # Победили боты или банка нет — платить некому, раунд считаем закрытым.
        state["is_paid"] = True
        state["payout_status"] = "empty"
        state["payout_error"] = ""
        return True

    paid_keys = set(state.get("paid_users") or [])
    _queue_payout_entries(arena, round_id, plan, paid_keys)
    state["payout_status"] = "pending"
    _drain_pending_payouts()
    if not state["is_paid"]:
        with PENDING_PAYOUTS_LOCK:
            left = [e for key, e in PENDING_PAYOUTS.items()
                    if key.startswith(f"{arena}:{round_id}:")]
        if left:
            state["payout_error"] = left[0].get("last_error") or state.get("payout_error", "")
    return bool(state["is_paid"])


def _resume_pending_payouts() -> int:
    """Подобрать долги из журнала arena_payouts (после рестарта и на всякий случай).

    В памяти раундов уже может не быть, а журнал знает, кому и сколько остались
    должны. Строки попадают в общую очередь и выплачиваются идемпотентно:
    arena_payout_apply переводит строку в paid и начисляет ровно один раз.
    """
    backend = _arena_payout_backend()
    if not backend["ledger"]:
        return 0
    try:
        rows = (supabase.table(ARENA_PAYOUTS_TABLE).select("*")
                .eq("status", "pending").order("created_at").limit(200).execute().data) or []
    except Exception as exc:  # noqa: BLE001
        logger.info("arena_payouts недоступна, доплат нет: %s", exc)
        return 0
    with PENDING_PAYOUTS_LOCK:
        for row in rows:
            try:
                uid = int(row.get("user_id"))
                amount = int(row.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            key = _pending_key(str(row.get("arena") or ""), str(row.get("round_id") or ""), uid)
            PENDING_PAYOUTS.setdefault(key, {
                "arena": str(row.get("arena") or ""),
                "round_id": str(row.get("round_id") or ""),
                "user_id": uid, "amount": amount,
                "attempts": 0, "last_error": "",
            })
    if rows:
        logger.info("Арена: из журнала подобрано %s незакрытых выплат", len(rows))
    return _drain_pending_payouts()


def _payout_hockey_winner():
    """Совместимость: выплатить по обоим котлам."""
    _payout_arena_winner("normal")
    _payout_arena_winner("team")


def _finish_hockey_round(arena: str = "normal"):
    """Разрешает ОДИН котёл по его собственной серверной финальной точке.

    Задача 42: раньше финиш закрывал сразу оба котла одной точкой броска —
    одиночная и командная всегда заканчивались одновременно. Теперь у каждой
    арены свой раунд, своя траектория и своя выплата.
    """
    state = _arena_state(arena)
    if state["status"] != "rolling":
        return False
    state["bank"] = sum(int(player["bet"]) for player in state["players"])
    roll_data = state.get("roll_data") or {}
    final_x = float(roll_data["final_x"]) if "final_x" in roll_data else None

    winner = _hockey_winner_at_x(final_x, arena) if final_x is not None else None
    state["arena_winners"] = {"normal": None, "team": None}
    state["arena_winners"][arena] = winner
    if arena == "normal":
        state["winner"] = winner
        state["winner_team"] = None
    else:
        state["winner"] = None
        state["winner_team"] = winner["id"] if winner else None

    state["payouts"] = []
    state["paid_users"] = []
    plan = _arena_payout_plan(arena)

    # Лента истории общая: пишем самую крупную выплату этого раунда.
    if plan:
        best = max(plan, key=lambda entry: int(entry.get("amount") or 0))
        history.insert(0, {
            "round_id": state["round_id"],
            "winner_name": f"{best.get('name') or 'игрок'}",
            "winner_team": (winner or {}).get("team") if arena == "team" else None,
            "amount": int(best.get("amount") or 0),
            "arena": arena,
            "players": [{"name": p["name"], "bet": p["bet"],
                         "is_winner": str(p["id"]) == str(best.get("user_id"))}
                        for p in state["players"]],
        })
        del history[10:]

    state["status"] = "finished"
    state["last_update"] = time.time()
    # Задача 47: выплату пытаемся сразу здесь же. Если база не ответит, раунд
    # останется «finished + payout pending», и следующий /api/hockey_state
    # повторит попытку — деньги не теряются.
    state["payout_status"] = "pending"
    state["payout_error"] = ""
    # Задача 47: результат раунда сразу в базу — после рестарта доиграем и заплатим.
    _save_arena_snapshot(arena)
    return True


@app.on_event("startup")
async def start_hockey_settlement_worker():
    async def settle_expired_rounds():
        # Задача 47: перезапуск Render стирал раунд вместе со ставками. Сначала
        # поднимаем снимки раундов из базы (доиграть или вернуть ставки), затем
        # доплачиваем всё, что осталось должны по журналу выплат.
        try:
            restored = await asyncio.to_thread(_restore_arena_rounds)
            if restored.get("resumed") or restored.get("refunded"):
                logger.info("Арена после рестарта: доигрываем %s, возвратов %s",
                            restored.get("resumed"), len(restored.get("refunded") or []))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Арена: восстановление раундов не удалось: %s", exc)
        try:
            await asyncio.to_thread(_resume_pending_payouts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Арена: доплаты после рестарта не удались: %s", exc)

        while True:
            await asyncio.sleep(0.2)
            # Задача 42: каждый котёл живёт своим раундом и закрывается сам.
            for arena in ARENA_VIEWS:
                state = _arena_state(arena)
                now = time.time()
                if state["status"] == "waiting":
                    # Раунд не должен «залипать» в наборе ставок, если все вышли
                    # из приложения: без опроса стартуем бросок сами.
                    wait_duration = (HOCKEY_TEAM_WAIT_DURATION if arena == "team"
                                     else HOCKEY_WAIT_DURATION)
                    if now - state["last_update"] >= wait_duration:
                        _begin_arena_roll(arena, now)
                    continue
                if state["status"] == "rolling":
                    # Задача 47: закрываем раунд строго по своим часам, даже
                    # если /api/hockey_state никто не опрашивает (из аппки
                    # вышли) — иначе раунд «сбрасывался» вместе со ставками.
                    if now - state["last_update"] >= _round_roll_duration(arena):
                        _finish_hockey_round(arena)
                        state = _arena_state(arena)
                    else:
                        continue
                if state["status"] == "finished" and not state["is_paid"]:
                    # Победитель может быть ботом или его может не быть — план
                    # всё равно проверяем: платим ровно тем, кто выиграл.
                    # Задача 47: выплата идёт в отдельном потоке (Supabase —
                    # блокирующие вызовы) и повторяется до подтверждения.
                    if state.get("payout_running"):
                        continue
                    state["payout_running"] = True
                    try:
                        await asyncio.to_thread(_payout_arena_winner, arena)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Арена %s: поток выплаты упал: %s", arena, exc)
                    finally:
                        _arena_state(arena)["payout_running"] = False
            # Снимок раундов: если процесс умрёт, раунд восстановится и
            # выплатится (или ставки вернутся), а не «сбросится».
            _save_arena_snapshots()
            # Долги прошлых раундов (и подобранные из журнала после рестарта):
            # раунда в памяти уже нет, но деньги всё ещё должны.
            if PENDING_PAYOUTS and time.time() - _LAST_PAYOUT_DRAIN["at"] >= 1.0:
                _LAST_PAYOUT_DRAIN["at"] = time.time()
                try:
                    await asyncio.to_thread(_drain_pending_payouts)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Арена: повтор выплат упал: %s", exc)

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
    """Забанен ли игрок. Недоступность таблицы bans не должна давать 500.

    Задача 57: проверка идёт ДО гейта «только админы», и если Supabase в этот
    момент не отвечает, игрок получал 500 вместо понятного 403/503. Теперь
    сбой базы логируется, а решение о доступе принимает гейт.
    """
    try:
        res = supabase.table("bans").select("*").eq("user_id", user_id).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bans недоступна (проверка бана пропущена): %s", exc)
        return
    if res.data: raise HTTPException(status_code=403, detail="Вы забанены за нарушение правил.")


def _crash_reset_waiting(now):
    global crash_state
    crash_state = {
        "status": "waiting", "timer": CRASH_WAIT, "multiplier": 1.0, "last_update": now,
        "crash_point": 0.0, "players": [], "bank": 0, "is_paid": False, "round_id": str(int(time.time())),
        "bots_added": 0, "target_bots": 0, "last_bot_added": 0, "status_start": now,
    }


class UserAuth(BaseModel): initData: str


class AdminMaintenanceRequest(BaseModel):
    """Задача 71: тумблер техработ из админ-панели."""
    initData: str
    enabled: bool = False


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
    # 'red'/'blue' — команда командной арены (имеет смысл только при arena='team').
    team: Optional[str] = None
    # Какая арена: 'normal' (полосы + мяч) или 'team' (красные/синие).
    # Задача 31: котлы независимы, поэтому в одном раунде можно поставить в оба.
    arena: Optional[str] = None
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


class UpgradeLogRequest(BaseModel):
    initData: str
    limit: Optional[int] = None       # /api/upgrade_pending: сколько последних раундов отдать


class UpgradeAck(BaseModel):
    initData: str
    log_id: Optional[int] = None


class UpgradeAction(BaseModel):
    initData: str
    request_id: Optional[str] = None
    # Задача 70: клиент присылает уже сыгранный раунд, чтобы забрать его
    # результат (ответ потерялся по сети). Ставка не списывается повторно.
    replay_of: Optional[str] = None
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
def _arena_block(arena: str) -> dict:
    """Готовый блок одной арены: клиент просто рисует state.arenas[view]."""
    state = _arena_state(arena)
    players = _arena_players(arena)
    winner = state.get("winner")
    roll_data = state.get("roll_data") or {}
    block = {
        "id": arena,
        "status": state["status"],
        "timer": state.get("timer", 0),
        "round_id": state.get("round_id"),
        "players": players,
        "sectors": _hockey_sectors(arena),
        "bank": _arena_bank(arena),
        "winner": winner,
        # Задача 47: у командной арены победитель — КОМАНДА. Раньше winner
        # оставался None (id команды лежал только в winner_team), а клиент
        # рисует плашку по winner — поэтому «Победитель: команда Красные»
        # не появлялся вообще. Теперь winner — объект команды, а не игрок.
        "winner_team": ((winner or {}).get("team") or state.get("winner_team")) if arena == "team" else None,
        "players_count": len(players),
        "rolling_started_at": state.get("rolling_started_at"),
        # Своя длительность броска: 9 с мяч / 5.5 с стрелка (2 с разгон + 3.5 с торможение).
        "roll_duration": _round_roll_duration(arena),
        "finish_hold": _round_finish_hold(arena),
        "paid": bool(state.get("is_paid")),
        # Задача 47: чем закончился payoff этого раунда. Клиент по этому полю
        # говорит «выигрыш зачислен» или «не удалось выплатить, пробуем снова».
        "payout_status": state.get("payout_status") or ("pending" if not state.get("is_paid") else "paid"),
        "payout_error": state.get("payout_error") or "",
    }
    if arena == "team":
        block["teams"] = _hockey_teams()
        team_id = block["winner_team"]
        block["winner"] = ({"id": team_id, "team": team_id, "is_team": True,
                            "name": ARENA_TEAMS[team_id]["name"], "color": ARENA_TEAMS[team_id]["color"]}
                           if team_id in ARENA_TEAMS else None)
        block["winner_name"] = None
    else:
        # Задача 47: у одиночной арены победитель — ИГРОК. Своё имя раунда,
        # чтобы клиент никогда не подставлял победителя другого режима.
        block["winner_name"] = (winner or {}).get("name") if winner else None
    if roll_data:
        # Траекторию мяча не отдаём целиком: она большая, клиент берёт её
        # отдельным запросом /api/hockey_roll/{round_id}.
        block["roll_data"] = {k: v for k, v in roll_data.items() if k != "path"}
    return block


def _hockey_state_response():
    """Состояние ДВУХ независимых арен: у каждой свой раунд, банк и победитель."""
    normal_state = _arena_state("normal")
    team_state = _arena_state("team")
    arenas = {"normal": _arena_block("normal"), "team": _arena_block("team")}
    normal_players = arenas["normal"]["players"]
    team_players = arenas["team"]["players"]

    # Совместимость со старым клиентом: «верхние» поля описывают одиночную
    # арену, а полные данные обеих лежат в arenas[...].
    # Задача 47: копируем ТОЛЬКО скалярные поля. Раньше dict(normal_state) тащил
    # наверх живые списки/словари раунда (players, winner, roll_data) — клиент
    # и старый код могли их же и мутировать, и «победитель» переезжал между
    # режимами. Теперь наверх уходят только копии простых значений.
    response = {key: value for key, value in normal_state.items()
                if isinstance(value, (str, int, float, bool, type(None)))}
    response["winner"] = dict(normal_state["winner"]) if normal_state.get("winner") else None
    response["players"] = [dict(player) for player in normal_players]
    response["payouts"] = [dict(entry) for entry in (normal_state.get("payouts") or [])]
    response["status"] = normal_state["status"]
    response["round_id"] = normal_state["round_id"]
    response["timer"] = normal_state.get("timer", 0)
    response["normal_status"] = normal_state["status"]
    response["team_status"] = team_state["status"]
    response["normal_round_id"] = normal_state["round_id"]
    response["team_round_id"] = team_state["round_id"]
    response["normal_timer"] = normal_state.get("timer", 0)
    response["team_timer"] = team_state.get("timer", 0)
    response["bank"] = arenas["normal"]["bank"] + arenas["team"]["bank"]
    response["normal_bank"] = arenas["normal"]["bank"]
    response["team_bank"] = arenas["team"]["bank"]
    response["normal_players"] = normal_players
    response["team_players"] = team_players
    response["players"] = normal_players
    response["sectors"] = _hockey_sectors("normal")
    response["teams"] = _hockey_teams()
    response["team_sectors"] = _hockey_sectors("team")
    # Задача 47: в «верхних» полях — ТОЛЬКО одиночная арена. Раньше сюда
    # попадал весь state одиночного котла, включая winner, и клиент мог
    # показать победителя другого режима.
    response["arena_winners"] = {"normal": normal_state.get("winner"),
                                 "team": (team_state.get("winner") or
                                          ({"id": team_state.get("winner_team"),
                                            "team": team_state.get("winner_team")}
                                           if team_state.get("winner_team") else None))}
    response["arenas"] = arenas
    response["mode"] = "both"
    response["payouts"] = (normal_state.get("payouts") or []) + (team_state.get("payouts") or [])
    if response.get("roll_data"):
        response["roll_data"] = {k: v for k, v in response["roll_data"].items() if k != "path"}
    response["server_time"] = time.time()
    # Длительности для клиента: он строит анимацию по серверным числам.
    response["roll_duration"] = _round_roll_duration("normal")
    response["team_roll_duration"] = _round_roll_duration("team")
    response["team_spin_up"] = HOCKEY_TEAM_SPIN_UP
    response["team_spin_down"] = HOCKEY_TEAM_SPIN_DOWN
    return response


def _begin_arena_roll(arena: str, now: float):
    """Переводит котёл из набора ставок в бросок (один раз на раунд)."""
    state = _arena_state(arena)
    state["bank"] = sum(int(player["bet"]) for player in state["players"])
    state["status"] = "rolling"
    state["last_update"] = now
    state["rolling_started_at"] = now
    _update_hockey_percentages(arena)
    _prepare_hockey_roll(now, arena)
    return state


def _tick_arena(arena: str, now: float):
    """Один шаг жизни котла: набор ботов → бросок → финиш → выплата → новый раунд.

    Задача 42: котлы независимы, поэтому у каждого свои часы, свой таймер
    ожидания и своя длительность броска (мяч 9 с, стрелка 8 с).
    """
    state = _arena_state(arena)
    wait_duration = HOCKEY_TEAM_WAIT_DURATION if arena == "team" else HOCKEY_WAIT_DURATION
    roll_duration = _round_roll_duration(arena)
    finish_hold = _round_finish_hold(arena)

    if state["status"] == "waiting":
        time_left = wait_duration - (now - state["last_update"])
        _maybe_add_hockey_bots(now, time_left)
        if time_left <= 0:
            # Фиксируем полный банк котла, включая ставки ботов.
            state = _begin_arena_roll(arena, now)

    elif state["status"] == "rolling":
        if now - state["last_update"] >= roll_duration:
            # Сервер сам закрывает раунд: выход из аппки не отменяет розыгрыш.
            _finish_hockey_round(arena)
            state = _arena_state(arena)

    elif state["status"] == "finished":
        # Задача 47: выплатой занимается ТОЛЬКО фоновый воркер (в отдельном
        # потоке). Раньше выплата шла прямо из запроса — и медленный/зависший
        # Supabase блокировал событийный цикл, из-за чего экран арены
        # «сбрасывался», а баланс не приходил.
        if now - state["last_update"] > finish_hold and state["is_paid"]:
            # Новый раунд именно этого котла. Другой котёл продолжает свой.
            fresh = _new_arena_state(arena=arena)
            fresh["last_update"] = now
            fresh["last_bot_added"] = now
            # Тот же слот-код, что и в _prepare_hockey_roll: раунды арен не
            # могут совпасть между собой даже в одну секунду.
            fresh["round_id"] = str(int(now) * 10 + (0 if arena == "normal" else 7))
            # Задача 47: новый раунд перезаписывает снимок только теперь, когда
            # прошлый закрыт по деньгам — незакрытый раунд в базе не теряется.
            _save_arena_snapshot(arena)
            # Задача 47: очередь выплат раунда своя — долги прошлого раунда
            # этого же котла новый раунд не сбрасывает, они уже в журнале.
            with PENDING_PAYOUTS_LOCK:
                fresh_round_id = str(fresh["round_id"])
                fresh["payout_status"] = "pending" if any(
                    key.startswith(f"{arena}:{fresh_round_id}:") for key in PENDING_PAYOUTS
                ) else "none"
            ARENAS[arena] = fresh
            state = fresh

    state["timer"] = (max(0, int(wait_duration - (now - state["last_update"])))
                      if state["status"] == "waiting" else 0)
    return state


@app.get("/api/hockey_state")
async def get_hockey_state(initData: str = ""):
    """Состояние обеих арен за один запрос — каждая живёт своим раундом.

    Задача 57: во время техработ состояние отдаём только админам, иначе игру
    можно «смотреть» и считать ботов в обход закрытого Mini App.
    """
    _gate_view(initData)
    now = time.time()
    for arena in ARENA_VIEWS:
        _tick_arena(arena, now)
    return _hockey_state_response()


@app.get("/api/hockey_roll/{round_id}")
async def get_hockey_roll_path(round_id: str, initData: str = ""):
    """Траектория мяча одиночной арены. У командной её нет — она крутит стрелку."""
    _gate_view(initData)
    for arena in ARENA_VIEWS:
        state = _arena_state(arena)
        roll_data = state.get("roll_data")
        if (str(state.get("round_id")) == str(round_id) and roll_data
                and state["status"] in {"rolling", "finished"}):
            return {"round_id": round_id, "arena": arena, "path": roll_data["path"],
                    "roll_duration": _round_roll_duration(arena)}
    raise HTTPException(status_code=404, detail="Траектория раунда не найдена")


@app.post("/api/place_bet")
async def place_bet(data: PlaceBet):
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)  # техработы: во время них играют только админы
    user_id = str(user["id"])

    check_ban(user_id)

    if data.amount <= 0:
        raise HTTPException(status_code=400, detail="Ставка должна быть больше нуля")
    # Задача 56: минимальная ставка в арене. Раньше можно было поставить 1 коин
    # и забрать долю банка с ботов — на скрине так и было. Теперь у арены свой
    # минимум: игра про доли банка, а не про «бесплатный лотерейный билет».
    if int(data.amount) < ARENA_MIN_BET:
        raise HTTPException(status_code=400,
                            detail=f"Минимальная ставка в арене {ARENA_MIN_BET} коинов")
    # Какая арена: если команда указана без arena — значит командная (старый клиент).
    team = (data.team or "").strip().lower() or None
    if team not in (None, "red", "blue"):
        raise HTTPException(status_code=400, detail="Неизвестная команда")
    arena = (data.arena or "").strip().lower() or None
    if arena is None:
        arena = "team" if team else "normal"
    if arena not in ARENA_VIEWS:
        raise HTTPException(status_code=400, detail="Неизвестная арена")
    if arena != "team":
        team = None                       # в обычной арене команд нет

    # Задача 42: у каждого котла свой раунд, поэтому и «ставки закрыты» — своё.
    arena_state_now = _tick_arena(arena, time.time())
    wait_duration = HOCKEY_TEAM_WAIT_DURATION if arena == "team" else HOCKEY_WAIT_DURATION
    if (arena_state_now["status"] != "waiting"
            or time.time() - arena_state_now["last_update"] >= wait_duration):
        raise HTTPException(
            status_code=400,
            detail=("Ставки закрыты — идёт бросок" if arena_state_now["status"] == "rolling"
                    else "Раунд закончился, ставки откроются через несколько секунд"))

    # Запись уникальна в пределах котла: можно иметь ставку и в обычной, и в
    # командной арене одновременно, но не две записи в одной.
    existing_player = next(
        (p for p in _arena_players(arena) if p["id"] == user_id and not p.get("is_bot")),
        None,
    )
    # Нельзя ставить за обе команды в одном котле: иначе игрок «страхуется»
    # и выигрывает всегда. Отказ понятный, чтобы UI мог подсветить кнопку.
    if (existing_player is not None and team
            and existing_player.get("team") and existing_player.get("team") != team):
        blocked = ARENA_TEAMS.get(existing_player["team"], {}).get("name", "другую команду")
        raise HTTPException(status_code=400, detail=f"Вы уже ставите за {blocked} — дождитесь конца раунда")
    # Лимит считается по ОБЩЕЙ ставке игрока в этом котле, а не по одному
    # запросу — иначе он обходится десятью ставками по 1 000.
    total_after_bet = int(existing_player["bet"] if existing_player else 0) + int(data.amount)
    _enforce_bet_limit(user_id, total_after_bet)
    if existing_player is None and len(_arena_players(arena)) >= ARENA_MAX_PLAYERS:
        raise HTTPException(status_code=400, detail="Комната заполнена")

    res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
    if not res.data: raise HTTPException(status_code=404, detail="User not found")
    _row = _primary_user_row(res.data)
    current_coins = _safe_int(_row.get("coins"), 0) if _row else 0
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Not enough coins")

    new_coins = current_coins - data.amount
    if not _write_primary_coins(user_id, new_coins):
        raise HTTPException(status_code=500, detail="Не удалось списать ставку")

    # Задача 74: ставка в арене + рекорд ставки для профиля.
    stats_note(user_id, coins_spent=data.amount, spent_arena=data.amount,
               best_arena_bet=data.amount)
    ledger_note(user_id, "bet", data.amount, "🏒 Арена")

    if existing_player is not None:
        existing_player["bet"] += data.amount
    else:
        name = user.get("username", user.get("first_name", f"id{user_id}"))
        avatar = user.get("photo_url", "")
        player_color = "#007aff"
        while any(p["color"] == player_color for p in _arena_players(arena)):
            player_color = f"hsl({random.randint(0, 360)}, 80%, 50%)"
        existing_player = {
            "id": user_id, "name": name, "avatar": avatar, "bet": data.amount,
            "color": ARENA_TEAMS[team]["color"] if team else player_color,
            "is_bot": False, "team": team, "arena": arena,
        }
        _arena_state(arena)["players"].append(existing_player)

    # One 10% chance per round when a human's accumulated bet reaches 1,000.
    arena_state_now = _arena_state(arena)
    if existing_player["bet"] >= 1000 and not arena_state_now["whale_decided"]:
        arena_state_now["whale_decided"] = True
        arena_state_now["whale_join"] = random.random() < 0.10
        if arena_state_now["whale_join"]:
            arena_state_now["whale_trigger_at"] = random.uniform(1.0, 2.0)

    # The displayed bank and payout use the same full pot.
    arena_state_now["bank"] = sum(int(player["bet"]) for player in arena_state_now["players"])
    _update_hockey_percentages(arena)
    return {"success": True, "new_balance": new_coins, "arena": arena,
            "round_id": arena_state_now.get("round_id"),
            "status": arena_state_now["status"]}


@app.post("/api/finish_hockey")
async def finish_hockey(data: FinishHockey):
    """Клиент не решает исход: он может лишь попросить закрыть уже истёкший раунд.

    Задача 42: закрываем только ту арену, о которой просит клиент (по умолчанию —
    обе), и только если её бросок уже закончился по серверному времени.
    """
    # Задача 57: закрыть истёкший раунд может только тот, кому можно играть.
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access_uid(_safe_int(user.get("id"), None))
    finished = []
    for arena in ARENA_VIEWS:
        state = _arena_state(arena)
        if state["status"] != "rolling":
            continue
        if time.time() - state["last_update"] < _round_roll_duration(arena):
            continue
        if _finish_hockey_round(arena):
            finished.append(arena)
    response = _hockey_state_response()
    return {"success": bool(finished), "finished": finished,
            "winner": response["arena_winners"]["normal"],
            "winner_team": response["arenas"]["team"]["winner_team"],
            "arena_winners": response["arena_winners"],
            "normal_bank": response["normal_bank"], "team_bank": response["team_bank"],
            "bank": response["bank"]}


@app.get("/api/hockey_history")
async def get_hockey_history(): return history


@app.post("/api/arena_mode")
async def arena_mode(data: ArenaModeRequest):
    """Совместимость со старым клиентом.

    Задача 31: обе арены живут одновременно, а какой из них смотрит игрок —
    решает клиент локально. Эндпоинт ничего не блокирует и ничего не ломает:
    раньше он запрещал переключение во время раунда и при живых ставках, из-за
    чего «посмотреть вторую арену, пока идёт катка» было невозможно.
    """
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    require_play_access(user)
    mode = (data.mode or "").strip().lower()
    if mode not in ARENA_VIEWS:
        raise HTTPException(status_code=400, detail="Неизвестный режим арены")
    return {"ok": True, "mode": "both", "view": mode,
            "teams": _hockey_teams(), "normal_players": _arena_players("normal"),
            "team_players": _arena_players("team")}


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
    _gate_view(initData)
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
    _row = _primary_user_row(res.data)
    current_coins = _safe_int(_row.get("coins"), 0) if _row else 0
    if current_coins < data.amount: raise HTTPException(status_code=400, detail="Недостаточно коинов")

    if not _write_primary_coins(user_id, current_coins - data.amount):
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
    # Задача 74: ставка в ракете + «самая большая ставка» для профиля.
    stats_note(user_id, coins_spent=data.amount, spent_crash=data.amount,
               best_crash_bet=data.amount)
    ledger_note(user_id, "bet", data.amount, "🚀 Ракета")
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
    _row = _primary_user_row(res.data)
    new_coins = (_safe_int(_row.get("coins"), 0) if _row else 0) + win_amount
    if not _write_primary_coins(user_id, new_coins):
        raise HTTPException(status_code=500, detail="Не удалось зачислить выигрыш")
    player["cashout_mult"] = cashout_mult
    # Задача 74: «сколько больше всего забрал в ракете» = выигрыш в коинах
    # (ставка × множитель). Считаем по деньгам, а не по множителю: игрок
    # сравнивает суммы, а не x2.1 против x3.0.
    stats_note(user_id, coins_earned=win_amount, won_crash=win_amount,
               best_crash_win=win_amount)
    ledger_note(user_id, "win", win_amount, f"🚀 Ракета x{cashout_mult:g}")
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
        raise HTTPException(status_code=400, detail=f"Минимальная ставка {MINES_MIN_BET} коинов")
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
    # Задача 74: ставка в профиле. «Проиграно» считается в момент, когда игра
    # закончилась без выплаты (мина), см. mines_open.
    stats_note(user_id, coins_spent=amount, spent_mines=amount, best_mines_bet=amount,
               mines_games=1)
    ledger_note(user_id, "bet", amount, f"💣 Мины · {mines_count} мин")
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
        # Задача 74: мина — единственный момент, когда ставка в минах проиграна.
        # В профиле это строка «Проиграно в минах».
        stats_note(user_id, lost_games=int(game.get("bet") or 0))
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
    stats_note(user_id, coins_earned=win_amount, won_mines=win_amount,
               best_mines_win=win_amount)
    ledger_note(user_id, "win", win_amount, "💣 Мины")
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

    # Задача 57: во время техработ профиль (и запись новых игроков) — только
    # админам. Исключение для забаненных выше: им нужен внятный текст про бан.
    # Ответ структурный (а не просто detail), иначе клиент принимал 403 за бан:
    # Mini App показывал «Вы забанены» и закрывался вместо экрана техработ.
    if not can_play(user_id):
        return JSONResponse(status_code=403, content={
            "ok": False,
            "detail": _access_denied_message(user_id),
            "maintenance": True,
            "admin_only": bool(app_closed_now()),
            "can_play": False,
            "is_admin": bool(is_admin),
            "maintenance_message": _access_denied_message(user_id),
        })

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
        if _backfill_user_created_at(user_id, backfill):
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
        "admin_only_app": app_closed_now(),
        "can_play": can_play(user_id) if user_id is not None else False,
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


# ==================== АУДИТ СХЕМЫ БАЗЫ ====================
# Один список колонок на API и бота — db_schema.py. По нему /api/db_audit
# проверяет базу и печатает готовый SQL: когда игрок видит «не хватает колонки»,
# админ за один запрос понимает, применилась ли миграция и что доделать.
@app.post("/api/db_audit")
async def db_audit(data: UserAuth):
    """Диагностика схемы: чего именно не хватает в базе и какой SQL выполнить."""
    _require_admin_api(data.initData)
    report = db_schema_audit(supabase)
    report["build"] = API_BUILD
    report["supabase_host"] = _supabase_host()
    return report


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


def _pending_claims_for(user_id) -> list:
    """Незабранные награды этого игрока в памяти: [(claim_id, запись), …]."""
    _pending_claim_gc()
    return [(claim_id, entry) for claim_id, entry in list(_PENDING_CHEST_CLAIMS.items())
            if str(entry.get("user_id")) == str(user_id)]


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

    # Задача 74: «сколько открыл сундуков» — считаем в момент открытия, а не
    # в момент «Забрать»: рулетка уже крутилась, попытка потрачена.
    stats_note(user_id, chests_opened=1, **({"golden_chests": 1} if chest_type == "gold" else {}))
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
        # Задача 74: награда из памяти — та же статистика, что и из базы.
        chest_won = reward_coins + UPGRADE_ITEM_PRICES.get(reward_item, 0) * reward_item_amount
        if chest_won:
            stats_note(user_id, coins_earned=chest_won, won_chests=chest_won)
            ledger_note(user_id, "chest", chest_won,
                        "🎁 " + ("Золотой сундук" if chest_type == "gold" else "Сундук"))
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
    # Задача 74: «сколько выпало из сундуков» — ценность награды в коинах.
    chest_won = reward_coins + UPGRADE_ITEM_PRICES.get(reward_item, 0) * reward_item_amount
    if chest_won:
        stats_note(user_id, coins_earned=chest_won, won_chests=chest_won)
        ledger_note(user_id, "chest", chest_won,
                    "🎁 " + ("Золотой сундук" if claim.get("chest_type") == "gold" else "Сундук"))

    print(f"СУНДУК: награда забрана user_id={user_id} +{reward_coins} коинов, "
          f"+{reward_keys} ключей, капсула={reward_item or 'нет'} x{reward_item_amount}",
          flush=True)
    return {"success": True, "already_claimed": False, "reward_coins": reward_coins,
            "reward_keys": reward_keys,
            "reward_item": reward_item, "reward_item_amount": reward_item_amount,
            "inventory": inventory, "new_balance": new_coins, "new_keys": new_keys}


@app.post("/api/chest_pending_claim")
async def chest_pending_claim(data: UserAuth):
    """Есть ли незабранная награда сундука — и сразу забрать её (задача 70).

    Заказчик: открыл сундук, вышел из аппки на анимации, вернулся — КД идёт,
    а награды нет. Награда ВСЕГДА лежала в chest_claims незабранной: её выдаёт
    только кнопка «Забрать», а клиент после перезахода про неё не спрашивал.

    Теперь: (1) при заходе клиент спрашивает этот эндпоинт, (2) если награда
    есть — зачисляем её сразу и возвращаем, что именно пришло.
    """
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = _safe_int(user.get("id"), None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    check_ban(str(user_id))

    try:
        pending = await asyncio.to_thread(_fetch_unclaimed_claims, user_id)
    except ChestUnavailable as exc:
        _log_chest_error("pending claim: таблица недоступна", exc, user_id)
        return {"ok": True, "available": False, "pending": 0}
    except Exception as exc:  # noqa: BLE001
        _log_chest_error("pending claim: чтение", exc, user_id)
        return {"ok": True, "available": False, "pending": 0}
    if not pending:
        # Задача 70: награда могла остаться ТОЛЬКО в памяти процесса (таблица
        # chest_claims недоступна, миграция не применена). Ищем и её — иначе
        # именно та награда, из-за которой игрок писал «сундук не выдался»,
        # так и осталась бы висеть.
        pending_mem = _pending_claims_for(user_id)
        if not pending_mem:
            return {"ok": True, "available": True, "pending": 0, "claimed": 0,
                    "reward_coins": 0, "reward_keys": 0, "items": []}
        pending = [{"id": claim_id, "reward_coins": int(item.get("coins") or 0),
                    "reward_keys": int(item.get("keys") or 0),
                    "reward_item": str(item.get("item") or ""),
                    "reward_item_amount": int(item.get("item_amount") or 0)}
                   for claim_id, item in pending_mem]

    totals = {"coins": 0, "keys": 0, "items": []}
    claimed = 0
    for item in pending:
        claim_id = item.get("id")
        if claim_id is None:
            continue
        try:
            # Повторяем ровно путь кнопки «Забрать»: занимаем и зачисляем.
            response = await claim_chest(ClaimChestRequest(initData=data.initData,
                                                          claim_id=claim_id))
        except HTTPException as exc:
            logger.warning("Автовыдача сундука id=%s не прошла: %s", claim_id, exc.detail)
            continue
        except Exception as exc:  # noqa: BLE001
            _log_chest_error("автовыдача сундука id=%s" % claim_id, exc, user_id)
            continue
        if response.get("already_claimed"):
            continue
        totals["coins"] += int(response.get("reward_coins") or 0)
        totals["keys"] += int(response.get("reward_keys") or 0)
        item_id = str(response.get("reward_item") or "")
        amount = int(response.get("reward_item_amount") or 0)
        if item_id and amount > 0:
            totals["items"].extend([item_id] * amount)
        claimed += 1
        totals["new_balance"] = response.get("new_balance")
        totals["new_keys"] = response.get("new_keys")

    return {"ok": True, "available": True, "pending": len(pending), "claimed": claimed,
            "reward_coins": totals["coins"], "reward_keys": totals["keys"],
            "items": totals["items"],
            "new_balance": totals.get("new_balance"), "new_keys": totals.get("new_keys"),
            "detail": ("Награда за сундук зачислена" if claimed else "Награду забрать не удалось")}


@app.post("/api/shop_items")
async def get_shop_items(data: ShopCatalogRequest):
    """Shop catalog for the current user. Admin-only capsules are never included for regular users,
    а распроданные по глобальному тиражу не показываются никому."""
    user = verify_telegram_data(data.initData)
    if not user: raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = int(user["id"])
    check_ban(str(user_id))
    # Задача 57: во время техработ витрина закрыта всем, кроме админов.
    require_play_access(user)
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
async def stock_debug(initData: str = ""):
    """Диагностика тиража капсул: доходит ли покупка до RPC и что отвечает база.

    Ничего не списывает (пробный вызов с несуществующим item_id). Открывается
    в браузере: https://<сервис>/api/stock_debug — сразу видно, применена ли
    SQL-функция, какая у неё сигнатура и какой остаток в таблице.
    Задача 57: открывается только с initData администратора.
    """
    _gate_view(initData)
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
    # Задача 74: расходы в магазине. Считаем по факту покупки: сервер уже
    # списал цену из каталога, значит это ровно то, что ушло из кошелька.
    _bought_price = int((SHOP_ITEMS.get(data.item_id) or {}).get("price") or 0)
    if _bought_price > 0:
        stats_note(user_id, coins_spent=_bought_price, spent_shop=_bought_price)
        item = SHOP_ITEMS.get(data.item_id) or {}
        ledger_note(user_id, "buy", _bought_price,
                    f"🛒 {item.get('short_name') or data.item_id}")
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
    # Задача 65: в тексте отказа сразу говорим, что выбрать. Раньше игрок
    # получал «выбери цель подороже» на экране, где цели выбрать было негде
    # (при ставке капсулой сетка целей была спрятана) — и апгрейд выглядел
    # сломанным: 100.00% на круге и отказ на кнопку.
    if visual_chance > UPGRADE_MAX_CHANCE:
        hint = ""
        if target in UPGRADE_ITEM_PRICES:
            dearer = sorted((price, name) for name, price in UPGRADE_ITEM_PRICES.items()
                            if price > from_value)
            if dearer:
                cheapest = dearer[0][1]
                hint = (f" — ближайшая дороже: «{UPGRADE_ITEM_TITLES.get(cheapest, cheapest)}»"
                        f" ({dearer[0][0]} коинов)")
            else:
                hint = " — эта капсула самая дорогая, поставь цель «Коины»"
        raise HTTPException(status_code=400,
                            detail=f"Шанс {visual_chance:.2f}% слишком высокий{hint}")
    if visual_chance < UPGRADE_MIN_CHANCE:
        raise HTTPException(status_code=400,
                            detail=f"Шанс {visual_chance:.2f}% слишком низкий — выбери цель подешевле")
    return win_amount, visual_chance


def _upgrade_read_user(user_id: int):
    """Читаем только нужные поля — никаких обращений к несуществующим колонкам (PGRST204).

    Задача 54: строк может быть несколько (старые дубли user_id). Апгрейд раньше
    брал ПЕРВУЮ попавшуюся, а профиль — строку с максимальным балансом. Если
    первой оказывалась «мёртвая» строка с 0, апгрейд списывал не с того баланса,
    который видит игрок, — и получалось «списали не сразу / не тот баланс».
    Теперь правило выбора то же, что в /api/user: строка с максимумом коинов.
    """
    # Задача 59: колонки id в старых базах может не быть (PGRST204 «нет колонки
    # id в users» роняло апгрейд целиком). Тогда читаем без неё: id нужен только
    # для «пиши именно в эту строку», а на базе без id строки и так уникальны.
    try:
        response = (
            supabase.table("users")
            .select("coins, inventory, id")
            .eq("user_id", user_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        column, table = _sql_error_column_table(str(exc))
        if column != "id" or (table and table != "users"):
            raise
        logger.warning("users.id нет в базе — апгрейд работает без него "
                       "(примените 20261129000000_full_schema_all_points.sql)")
        response = (
            supabase.table("users")
            .select("coins, inventory")
            .eq("user_id", user_id)
            .execute()
        )
    if not response.data:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    rows = response.data
    row = max(rows, key=lambda item: _safe_int(item.get("coins"), 0))
    if len(rows) > 1:
        logger.warning("Апгрейд: у user_id=%s %s строк профиля, беру строку с балансом %s",
                       user_id, len(rows), _safe_int(row.get("coins"), 0))
    raw_coins = row.get("coins") or 0
    try:
        coins = int(raw_coins)
    except (TypeError, ValueError):
        coins = 0
    return coins, row.get("inventory"), row.get("id")


def _pg_filter_safe(value) -> bool:
    """Можно ли безопасно передать значение в фильтр PostgREST.

    Оставлено для обратной совместимости с другими вызовами. В апгрейде этот
    фильтр больше НЕ используется (задача 69): подробности в
    `_upgrade_write_user`.
    """
    if not isinstance(value, str):
        return False
    return not any(char in value for char in ',:()"')


# Причина последней неудачной записи (см. _upgrade_write_user, задача 69).
_UPGRADE_WRITE_ERROR = None


def _upgrade_write_user(user_id: int, raw_coins: int, raw_inventory, coins: int, inventory: list,
                        row_id=None) -> bool:
    """Оптимистичная запись результата апгрейда.

    Применяется, только если баланс не изменился с момента чтения. Ошибка
    сети/PostgREST — это False, а не 500: выше стоит повторное чтение и ещё
    одна попытка.

    Задача 54: если известен id строки, пишем ИМЕННО в неё. Раньше update шёл по
    всем строкам с этим user_id, и при старых дублях можно было перезаписать не
    ту запись (игрок видел баланс одной строки, а менялась другая).

    Задача 69: убран фильтр по инвентарю — из-за него апгрейд отвечал «баланс
    изменился во время раунда», хотя баланс никто не трогал. Причины:

      * inventory в базе — колонка JSONB. Фильтр `inventory=eq.<json-строка>`
        уходил как текст («["capsule"]»), а PostgREST сравнивает его с jsonb:
        запрос либо не находил ни одной строки (0 обновлений → заказчик видел
        ошибку на ПЕРВОМ же апгрейде), либо падал с ошибкой типов;
      * после первой же записи Postgres хранит jsonb в своём формате
        (пробелы, порядок), и прочитанная строка перестаёт совпадать с тем,
        что мы передаём обратно — снова 0 обновлений.

    Инвентарь по-прежнему защищён: мы читали его в той же транзакции, что и
    баланс, а любой другой апгрейд или игра меняют `coins` (у апгрейда баланс
    меняется всегда — ставка списывается до броска). Если инвентарь поменялся,
    а баланс нет, пишем по `id` конкретной строки — это ровно та строка,
    которую читали.
    """
    update_values = {"coins": coins, "inventory": json.dumps(inventory, ensure_ascii=False)}
    query = supabase.table("users").update(update_values).eq("user_id", user_id).eq("coins", raw_coins)
    # Задача 59: пишем по id только когда он реально есть (на старой базе его нет).
    if row_id is not None and str(row_id).strip() not in ("", "None"):
        query = query.eq("id", row_id)
    try:
        updated = query.select("user_id").execute()
    except Exception as exc:  # noqa: BLE001 — текст ошибки нужен игроку
        logger.warning("Не удалось записать результат апгрейда", exc_info=True)
        # Задача 69: техническая причина (нет колонки, RLS, тип) — это не
        # «кто-то поменял баланс». Запоминаем причину, чтобы 409 сказал правду.
        global _UPGRADE_WRITE_ERROR
        _UPGRADE_WRITE_ERROR = _upgrade_missing_table_text(exc)
        return False
    _UPGRADE_WRITE_ERROR = None
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
        # Задача 54: «до» и «после» вместе нужны клиенту, чтобы показать ровно
        # то, что списал сервер (и не дать проигрышу «потеряться»).
        "coins_before": _safe_int(row.get("coins_before"), 0),
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


def _upgrade_missing_table_text(exc) -> str:
    """Текст ошибки базы → что именно чинить. Задача 58.

    На скрине игрок видел «Не удалось запустить апгрейд» — общую фразу без
    причины: исключение Supabase превращалось в 500 и терялось. Теперь
    причины разные и по каждой видно, что делать.

    Задача 59: колонку и таблицу берём ИЗ САМОГО ответа базы. Раньше текст
    всегда говорил «нет колонки в users», даже если дело было в upgrade_log
    или chest_claims, и админ правил не то.
    """
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    if "upgrade_log" in low:
        return "upgrade_log: примените миграцию 20261029000000_upgrade_log.sql"
    if "idempotency_keys" in low:
        return "idempotency_keys: примените миграцию 20261101000000_server_rls_policies.sql"
    if "row-level security" in low or "42501" in low or "permission denied" in low:
        return "RLS: примените 20261129000000_full_schema_all_points.sql (там политики и grant)"
    if "pgrst204" in low or "42703" in low or "column" in low:
        column, table = _sql_error_column_table(text)
        where = f"{table or 'таблице'}»" if column else "одной из таблиц"
        if column:
            return (f"нет колонки «{column}» в {where}: примените "
                    "20261129000000_full_schema_all_points.sql")
        return "нет колонки: примените 20261129000000_full_schema_all_points.sql"
    if "pgrst205" in low or "does not exist" in low or "schema cache" in low:
        return "нет таблицы/кэш схемы: выполните NOTIFY pgrst, 'reload schema'"
    return text[:200]


@app.post("/api/upgrade_action")
async def upgrade_action(data: UpgradeAction):
    """Апгрейд предмета: слева ставка, справа цель. Шанс = 90 / X."""
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")

    user_id = int(user["id"])
    # Задача 57: техработы закрывают апгрейд для всех, кроме админов.
    require_play_access_uid(user_id)
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

    # Задача 70: «Сервер не ответил, а ставка сгорела». Клиент, не дождавшись
    # ответа, присылает replay_of=<request_id прошлого раунда>: раунд уже
    # сыгран и записан, надо только вернуть его результат. Ставка не
    # списывается, баланс не двигается — ответ берётся из журнала идемпотентности
    # (или из upgrade_log, если ключ успел истечь).
    if data.replay_of:
        replay = _upgrade_replay(user_id, str(data.replay_of))
        if replay is not None:
            replay["replay"] = True
            return replay
        raise HTTPException(status_code=404,
                            detail="Раунд не найден — ставка не списана, можно ставить заново")

    try:
        source, amount, target, _ = _upgrade_normalize_request(data)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — мусор в запросе не должен давать 500
        logger.warning("Апгрейд: некорректный запрос (%s)", exc)
        raise HTTPException(status_code=400, detail="Некорректный запрос апгрейда")

    if source == "coin":
        if amount < UPGRADE_MIN_BET:
            raise HTTPException(status_code=400, detail=f"Минимальная ставка {UPGRADE_MIN_BET} коинов")
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
        # Задача 58: ошибка чтения профиля больше не превращается в 500 без
        # текста. Раньше игра отвечала «Не удалось запустить апгрейд», потому
        # что исключение базы уходило наверх пустым.
        try:
            raw_coins, raw_inventory, row_id = _upgrade_read_user(user_id)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Апгрейд: не удалось прочитать профиль %s: %s", user_id, exc)
            raise HTTPException(status_code=503,
                                detail="База не отвечает: " + _upgrade_missing_table_text(exc))
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

        # Шанс = ровно 90 / X, без дом-эджа и без множителей из game_settings.
        # Раньше их можно было подкрутить кнопкой «Шансы апгрейда» в админке:
        # игрок видел один процент, а бросок шёл по другому. Теперь это одно
        # число и на сервере, и в круге.
        real_chance = visual_chance
        if not admin:
            divider = float(UPGRADE_NON_ADMIN_DIVIDER) if UPGRADE_NON_ADMIN_DIVIDER else 1.0
            # Задача 54: защита от опечатки в переменной окружения. Делитель 0
            # раньше давал ZeroDivisionError → 500, и игрок видел «ставка не
            # списалась», хотя запрос уже ушёл.
            if not (0.01 <= divider <= 100):
                logger.warning("UPGRADE_NON_ADMIN_DIVIDER=%r вне диапазона — играю как 1.0",
                               UPGRADE_NON_ADMIN_DIVIDER)
                divider = 1.0
            real_chance /= divider

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

        if _upgrade_write_user(user_id, raw_coins, raw_inventory, new_coins, new_inventory, row_id):
            # Задача 74: «сколько потратил в апгрейде» и «лучший апгрейд» — из
            # факта розыгрыша (баланс уже записан, значит ставка реально ушла).
            # Апгрейд не «проигрыш в игре»: ставка всегда уходит из кошелька
            # (coins_spent), а проигранной становится только при неудаче.
            stake_value = _upgrade_source_value(source, amount)
            moment_text, moment_from, moment_to = _upgrade_moment(source, amount, target)
            deltas = {"coins_spent": stake_value, "spent_upgrade": stake_value,
                      "upgrades_total": 1}
            if won:
                deltas["upgrades_won"] = 1
            else:
                deltas["lost_games"] = stake_value
            stats_note(user_id, **deltas)
            ledger_note(user_id, "bet", stake_value, f"⚗️ Апгрейд · {moment_text}")
            if won:
                won_value = payout if target == "coin" else UPGRADE_ITEM_PRICES.get(target, 0)
                stats_note(user_id, coins_earned=won_value, won_upgrade=won_value)
                ledger_note(user_id, "win", won_value, f"⚗️ Апгрейд · {moment_text}")
                stats_event(user_id, "upgrade", moment_text,
                            max(moment_from, won_value),
                            {"from": source, "amount": amount, "to": target,
                             "coins_from": moment_from,
                             "coins_to": won_value if target == "coin" else moment_to})
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
                "house_edge": 1.0,   # дом-эджа больше нет: шанс фиксирован 90/X
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

    # Задача 58: ни одна из попыток не записалась. Ставка НЕ списана — говорим
    # об этом прямо, иначе игрок не понимает, вернулись ли коины.
    # Задача 69: если причина техническая (база не приняла запись), нельзя
    # врать про «баланс изменился» — это сбивало заказчика с толку: баланс на
    # экране не менялся, а текст утверждал обратное.
    if _UPGRADE_WRITE_ERROR:
        logger.warning("Апгрейд не записан (user_id=%s): %s", user_id, _UPGRADE_WRITE_ERROR)
        raise HTTPException(
            status_code=409,
            detail=f"Ставка не списана: база не приняла запись. Попробуйте ещё раз ({_UPGRADE_WRITE_ERROR})")
    raise HTTPException(
        status_code=409,
        detail="Ставка не списана: баланс изменился во время раунда. Попробуйте ещё раз")


def _upgrade_replay(user_id: int, request_id: str):
    """Вернуть результат уже сыгранного раунда по его request_id (задача 70).

    Два источника: журнал идемпотентности (там лежит ровно тот ответ, который
    получил бы клиент) и upgrade_log (если ключ истёк или таблица недоступна).
    None — раунда нет, значит ставка НЕ списывалась.
    """
    key = _idempotency_key(user_id, request_id, "upgrade")
    cached = idempotency_lookup(key)
    if cached is not None:
        return cached
    # Журнал идемпотентности — единственный точный источник: в нём лежит ровно
    # тот ответ, который должен был получить клиент. Если ключа нет (истёк TTL
    # или таблица недоступна), НЕ подставляем «какой-нибудь» раунд из лога:
    # так игрок мог получить чужой (уже показанный) результат. Пусть повторит —
    # ставка в этом случае не списана.
    return None


@app.post("/api/upgrade_pending")
async def upgrade_pending(data: UpgradeLogRequest):
    """Последние раунды апгрейда — клиент сверяет по ним баланс (задача 54).

    Зачем: игрок нажимает АПГРЕЙД, закрывает аппку/выходит в меню и заходит
    снова. В памяти приложения баланс «до ставки», и он видит старую сумму.
    Здесь отдаются раунды за последние минуты (по умолчанию 10): по ним клиент
    подтягивает реальный баланс и, если раунд уже сыгран, показывает результат.
    """
    user = verify_telegram_data(data.initData)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth data")
    user_id = _safe_int(user.get("id"), None)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Некорректный id пользователя")
    # Задача 57: апгрейд — часть игры, значит во время техработ только админам.
    require_play_access_uid(user_id)

    limit = max(1, min(50, _safe_int(getattr(data, "limit", None), 10) or 10))
    rows = None
    try:
        res = (supabase.table("upgrade_log")
               .select("*")
               .eq("user_id", user_id)
               .order("id", desc=True)
               .limit(limit)
               .execute())
        rows = res.data or []
    except Exception:  # noqa: BLE001
        rows = None

    # Задача 60: баланс берём из users, а НЕ из coins_after последнего раунда.
    # Раньше клиент на входе в Апгрейд подтягивал «coins» = coins_after старой
    # записи журнала. Если последний записанный раунд был проигран «в ноль»
    # (или coins_after не записался и превратился в 0), игрок видел 0 при
    # настоящем балансе — и лечилось это только перезагрузкой страницы.
    coins = None
    try:
        row = _db_user(user_id)
        if row:
            coins = _safe_int(row.get("coins"), 0)
    except Exception:  # noqa: BLE001
        coins = None

    if rows is None:
        # Таблицы нет — отдаём хотя бы баланс, чтобы клиент мог его сверить.
        return {"ok": False, "available": False, "coins": coins, "rounds": []}

    latest = rows[0] if rows else None
    if coins is None:
        # users недоступен — тогда единственное, что у нас есть, это журнал.
        coins = _safe_int(latest.get("coins_after"), 0) if latest else None
    return {
        "ok": True,
        "available": True,
        "coins": coins,
        "coins_from": "users",
        "server_time": int(time.time()),
        "rounds": [_upgrade_log_public(row) for row in rows],
    }


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
    require_play_access_uid(user_id)  # задача 57: техработы → только админы
    if UPGRADE_ADMIN_ONLY and not is_admin_id(user_id, ADMIN_IDS):
        raise HTTPException(status_code=403, detail=UPGRADE_ADMIN_ONLY_MESSAGE)

    # Задача 60: живой баланс из users. Клиент показывает «пропущенный» раунд
    # и берёт из него coins_after — а журнал мог не записаться (тогда там 0) или
    # устареть после других игр. Без этого игрок видел 0 после показа результата.
    live_coins = None
    try:
        user_row = _db_user(user_id)
        if user_row:
            live_coins = _safe_int(user_row.get("coins"), 0)
    except Exception:  # noqa: BLE001
        live_coins = None

    rows = _upgrade_log_rows(user_id)
    if rows is None:
        return {"available": False, "stats": None, "last_unseen": None,
                "coins": live_coins,
                "detail": "Таблица upgrade_log ещё не создана — примените миграцию"}
    last_unseen = next((row for row in rows if not row.get("seen")), None)
    return {
        "available": True,
        "coins": live_coins,
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
    require_play_access_uid(user_id)  # задача 57: техработы → только админы
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
    _row = _primary_user_row(res.data)
    current_coins = _safe_int(_row.get("coins"), 0) if _row else 0
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


async def _send_user_dm(user_id, text: str, reply_markup=None) -> bool:
    """Личное сообщение от бота игроку. Лучший вариант: не роняем запрос.

    Нужно для задания «подписка на канал» (задача 35) и подтверждения бонуса:
    игрок должен увидеть в боте кнопку «Проверить подписку», а после начисления —
    что награда пришла, даже если задание из Mini App уже исчезло.
    """
    if not BOT_TOKEN:
        logger.warning("DM не отправлено: BOT_TOKEN не задан (user_id=%s)", user_id)
        return False
    body = {"chat_id": int(user_id), "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        body["reply_markup"] = reply_markup
    try:
        payload = await _http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                   method="POST", payload=body, timeout=8)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DM не отправлено (user_id=%s): %s", user_id, exc)
        return False
    if isinstance(payload, dict) and payload.get("ok"):
        return True
    logger.warning("DM отказ (user_id=%s): %s", user_id,
                   (payload or {}).get("description") if isinstance(payload, dict) else payload)
    return False


def _subscription_claimed(user_id) -> Optional[bool]:
    """True/False — было ли начисление. None — база не дала прочитать.

    Читаем оба источника: основную таблицу и запасную отметку в game_settings.
    Запасную ставит бот (bot.py), если миграция со subscriptions_history ещё не
    применена, — иначе приложение продолжало бы показывать выполненное задание.
    """
    uid = int(user_id)
    saw_table = False
    try:
        # Только user_id: если колонки bonus_claimed в старой схеме нет,
        # select по ней падал и отметка «получено» терялась.
        res = (supabase.table(SUBSCRIPTIONS_TABLE).select("user_id")
               .eq("user_id", uid).limit(1).execute())
        saw_table = True
        if res.data:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("subscriptions_history недоступна: %s", exc)
    # Задача 44: ВСЕГДА проверяем запасную отметку в game_settings, а не только
    # при ошибке таблицы. Бот пишет её, если insert в subscriptions_history не
    # прошёл (RLS/нет колонки) — из-за этого плашка «+200 коинов» появлялась
    # снова при повторном входе в аппку.
    try:
        res = (supabase.table("game_settings").select("value")
               .eq("key", f"sub_bonus_{uid}").limit(1).execute())
        if res.data and str(res.data[0].get("value") or "").strip():
            return True
        if saw_table:
            return False
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("game_settings недоступна для отметки бонуса: %s", exc)
        return None if not saw_table else False


def _channel_status_payload(uid) -> dict:
    """Статус задания «подписка на канал» — без выдачи бонуса."""
    claimed = _subscription_claimed(uid)
    return {
        "ok": claimed is not None,
        "claimed": bool(claimed),
        "bonus_claimed": bool(claimed),
        "bonus": SUBSCRIPTION_BONUS,
        "channel_url": CHANNEL_URL,
        "channel": f"@{CHANNEL_USERNAME}",
        "detail": ("" if claimed is None else
                   ("Бонус за подписку уже получен" if claimed else
                    "Задание ждёт выполнения в личке бота")),
    }


@app.post("/api/channel_status")
async def channel_status(data: ChannelCheckRequest):
    """Лёгкая проверка: получен ли бонус за подписку.

    Mini App по ней скрывает плашку задания. Сам бонус тут НЕ выдаётся.
    """
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: техработы → только админы
    return _channel_status_payload(uid)


@app.post("/api/channel_task")
async def channel_task(data: ChannelCheckRequest):
    """Задание «подписка на канал» уходит игроку в личку бота.

    Задача 35: раньше бонус начислялся по тапу на плашку в Mini App. Теперь
    плашка только открывает канал и просит бота прислать в ЛС сообщение с
    кнопкой «Проверить подписку». Начисление и удаление задания делает бот
    (bot.py → cb_sub_check), поэтому бонус по-прежнему одноразовый: отметка
    пишется в subscriptions_history.
    """
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: задание не выдаём во время техработ
    status = _channel_status_payload(uid)
    if status["claimed"]:
        return {**status, "dm_sent": False, "sent": False}

    text = (
        "📢 <b>Задание: подписка на канал</b>\n\n"
        f"1. Подпишись на канал {CHANNEL_URL}\n"
        "2. Вернись сюда и нажми кнопку ниже\n\n"
        f"🎁 Награда: <b>+{SUBSCRIPTION_BONUS} коинов</b> (один раз).\n"
        "<i>После начисления задание исчезнет из приложения.</i>"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Проверить подписку", "callback_data": f"sub_check|{uid}"}
    ], [
        {"text": "📢 Открыть канал", "url": CHANNEL_URL}
    ]]}
    dm_sent = await _send_user_dm(uid, text, reply_markup=keyboard)
    return {**status, "ok": dm_sent, "dm_sent": dm_sent, "sent": dm_sent,
            "detail": ("Задание отправлено в личку бота" if dm_sent else
                       "Бот не смог написать в личку: открой бота и нажми «Старт»")}


@app.post("/api/channel_check")
async def channel_check(data: ChannelCheckRequest):
    """Совместимость со старым клиентом: теперь это только СТАТУС задания.

    Бонус здесь больше не начисляется — его выдаёт кнопка «Проверить подписку»
    в личке бота (см. /api/channel_task и bot.py → cb_sub_check).
    """
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: техработы → только админы
    return _channel_status_payload(uid)


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


# Резерв, если таблицы passive_income нет (миграция не применена): держим
# отметку времени в памяти процесса. Раньше в этом случае игрок получал окно
# «Примените миграцию» и не мог забрать пассивку вообще. С памятью доход
# работает, а после перезапуска сервиса просто отсчитывается заново — начислить
# лишнего так невозможно.
_PASSIVE_MEMORY: dict = {}
_PASSIVE_WARNED = {"once": False}


def _passive_last_at(user_id):
    """Момент последнего начисления. None — не начисляли, 'error' — нет таблицы.

    Задача 44: при недоступной базе возвращаем отметку из ПАМЯТИ. Раньше всегда
    возвращалось 'error', а вызывающий код читал память только если она уже
    была — в результате на КАЖДОМ заходе `last` считался пустым, отметка
    ставилась на «сейчас», и накопление никогда не начиналось (игрок видел
    «Накоплено 0», хотя капсулы есть). Теперь память — полноценный источник.
    """
    uid = int(user_id)
    try:
        res = (supabase.table(PASSIVE_INCOME_TABLE).select("last_at")
               .eq("user_id", uid).limit(1).execute())
        if res.data:
            parsed = _parse_timestamp(res.data[0].get("last_at"))
            if parsed:
                _PASSIVE_MEMORY.setdefault(uid, float(parsed))
                return float(parsed)
            return None
    except Exception as exc:  # noqa: BLE001
        if not _PASSIVE_WARNED["once"]:
            logger.warning("passive_income недоступна (работаю на памяти): %s", exc)
            _PASSIVE_WARNED["once"] = True
    # База пуста ИЛИ недоступна — но если процесс уже видел начисление, время
    # идёт от него. Это и есть «копить, пока игрок офлайн».
    return _PASSIVE_MEMORY.get(uid, "error")


def _passive_touch(user_id, stamp: float) -> bool:
    # Память пишем всегда: если база недоступна, отметка всё равно не потеряется
    # до перезапуска процесса.
    _PASSIVE_MEMORY[int(user_id)] = float(stamp)
    try:
        supabase.table(PASSIVE_INCOME_TABLE).upsert(
            {"user_id": int(user_id), "last_at": _iso_from_timestamp(stamp)},
            on_conflict="user_id").execute()
        return True
    except Exception as exc:  # noqa: BLE001
        if not _PASSIVE_WARNED["once"]:
            logger.warning("Не удалось сохранить passive_income %s: %s", user_id, exc)
            _PASSIVE_WARNED["once"] = True
        return False


def _iso_from_timestamp(stamp: float) -> str:
    return datetime.datetime.fromtimestamp(float(stamp), datetime.timezone.utc).isoformat()


def _passive_multiplier_at(stamp: float) -> float:
    """Множитель ставки в конкретную секунду: ночью по МСК — 0.5, днём — 1.0."""
    msk_hour = ((float(stamp) + MSK_OFFSET_SECONDS) % DAY_SECONDS) / 3600.0
    is_night = (msk_hour >= PASSIVE_NIGHT_START_HOUR) or (msk_hour < PASSIVE_NIGHT_END_HOUR)
    return PASSIVE_NIGHT_MULTIPLIER if is_night else PASSIVE_DAY_MULTIPLIER


def _passive_split_hours(start: float, end: float) -> tuple:
    """Разбивает интервал на «день/ночь» и считает (реальные, оплачиваемые) часы.

    Возвращает (real_hours, effective_hours), где effective_hours — реальные
    часы, умноженные на ночную ставку. Так ночь с 22 до 8 даёт ровно вдвое
    меньше, а интервал, попадающий на границу, считается честно по кускам.
    """
    start, end = float(start), float(end)
    real_hours = max(0.0, (end - start) / 3600.0)
    if real_hours <= 0:
        return 0.0, 0.0

    effective = 0.0
    cursor = start
    while cursor < end - 1e-6:
        # Ближайшая граница «ночь → день» или «день → ночь» по МСК.
        msk_now = (cursor + MSK_OFFSET_SECONDS) % DAY_SECONDS
        hour = msk_now / 3600.0
        night = (hour >= PASSIVE_NIGHT_START_HOUR) or (hour < PASSIVE_NIGHT_END_HOUR)
        if night:
            # До 08:00, если мы уже после 22:00 — значит до конца суток.
            next_boundary = (PASSIVE_NIGHT_END_HOUR * 3600.0) if hour < PASSIVE_NIGHT_END_HOUR \
                else DAY_SECONDS
        else:
            next_boundary = PASSIVE_NIGHT_START_HOUR * 3600.0
        available = DAY_SECONDS - msk_now
        step = min(end - cursor, max(1e-6, next_boundary - msk_now), available)
        multiplier = PASSIVE_NIGHT_MULTIPLIER if night else PASSIVE_DAY_MULTIPLIER
        effective += step / 3600.0 * multiplier
        cursor += step
    return real_hours, round(effective, 6)


def _passive_preview(start: float, now: float) -> dict:
    """Сколько уже накапало (для карточки пассивки), без начисления."""
    window_end = min(float(now), float(start) + PASSIVE_INCOME_MAX_HOURS * 3600.0)
    real_hours, effective_hours = _passive_split_hours(start, window_end)
    return {"real_hours": round(real_hours, 3), "hours": round(effective_hours, 3),
            "capped": (now - start) / 3600.0 >= PASSIVE_INCOME_MAX_HOURS}


def _passive_start_counter(user_id, rate: float) -> None:
    """Запустить отсчёт пассивки, если он ещё не запущен (без начисления).

    Задача 48: раньше отметку `last_at` ставило ТОЛЬКО начисление
    (`POST /api/passive_income`), а кнопка «Забрать» недоступна, пока не
    накопился целый коин. Получался замкнутый круг: отметки нет → накоплено 0 →
    кнопка заблокирована → отметка никогда не появится → **пассивка не капала
    ни минуты** (игрок видел «130.1 в час» и «0 накоплено» через 9 минут).

    Здесь мы просто ставим точку отсчёта: ни один коин не начисляется, «Забрать»
    по-прежнему только по кнопке. Отметка ставится один раз на профиль.
    """
    if rate <= 0:
        return
    try:
        if _passive_last_at(user_id) not in (None, "error"):
            return
    except Exception:  # noqa: BLE001 — чтение не должно ломать профиль
        return
    _passive_touch(user_id, time.time())


def _passive_pending_sync(user_id) -> dict:
    """Сколько пассивного дохода УЖЕ накопилось (для карточки в профиле).

    Задача 42: раньше карточка показывала только «130.1 коина в час», и игрок
    не понимал, сколько уже капнуло. Считаем ровно по той же формуле, что и
    начисление (`/api/passive_income`), но НИЧЕГО не начисляем: отметку времени
    двигает только кнопка «Забрать».
    """
    result = {"pending": 0.0, "rate": 0.0, "hours": 0.0, "real_hours": 0.0,
              "capped": False, "night": False, "since": None, "max_hours": PASSIVE_INCOME_MAX_HOURS}
    try:
        res = (supabase.table("users").select("inventory")
               .eq("user_id", int(user_id)).limit(1).execute())
        if not res.data:
            return result
        inventory = parse_inventory(res.data[0].get("inventory"))
    except Exception:  # noqa: BLE001 — профиль должен открываться даже без базы
        return result
    rate, _ = _passive_rate_for(inventory)
    result["rate"] = rate
    # Задача 56: мелочь с прошлого забора участвует в выплате.
    # Задача 56 (повторная проверка): в `/api/passive_income` мелочь
    # прибавляется ВСЕГДА (`entitled = rate * hours + carry`), поэтому и здесь
    # считаем её даже без капсул. Раньше при `rate <= 0` выходили с нулём, и
    # накопленная мелочь пропадала из карточки (а кнопка «Забрать» не работала
    # вовсе, если вывести последнюю капсулу).
    try:
        carry_ahead = float(_passive_carry_get(user_id) or 0.0)
    except Exception:  # noqa: BLE001
        carry_ahead = 0.0
    if rate <= 0:
        result["pending"] = round(carry_ahead, 3)
        return result
    # Задача 48: первый заход с капсулами запускает отсчёт (коины не начисляются).
    _passive_start_counter(user_id, rate)

    now = time.time()
    last = _passive_last_at(user_id)
    if last == "error" or last is None:
        last = _PASSIVE_MEMORY.get(int(user_id))
    if last is None:
        # Отметки ещё нет: начисление только начнётся, копить пока нечего.
        # Но мелочь с прошлого раза уже можно забрать — её показываем.
        result["pending"] = round(carry_ahead, 3)
        result["since"] = now
        return result

    window_end = min(now, float(last) + PASSIVE_INCOME_MAX_HOURS * 3600.0)
    real_hours, hours = _passive_split_hours(float(last), window_end)
    result.update({
        "pending": round(rate * hours + carry_ahead, 3),
        "hours": round(hours, 3),
        "real_hours": round(real_hours, 3),
        "capped": (now - float(last)) / 3600.0 >= PASSIVE_INCOME_MAX_HOURS,
        "night": _passive_multiplier_at(now) == PASSIVE_NIGHT_MULTIPLIER,
        "since": float(last),
    })
    return result


async def _passive_pending(user_id) -> dict:
    return await asyncio.to_thread(_passive_pending_sync, user_id)


def _passive_pending_public(user_id) -> dict:
    """Компактный блок «сколько накопил» для ответа /api/user.

    Отдельно от полного _passive_pending_sync: клиенту нужны только сумма,
    ставка и признак «упёрлись в 24 часа».
    """
    if user_id is None:
        return {"amount": 0.0, "whole": 0, "rate": 0.0, "hours": 0.0,
                "capped": False, "night": False, "max_hours": PASSIVE_INCOME_MAX_HOURS}
    try:
        pending = _passive_pending_sync(user_id)
    except Exception:  # noqa: BLE001 — профиль не должен падать из-за пассивки
        logger.debug("passive pending недоступен для %s", user_id, exc_info=True)
        return {"amount": 0.0, "whole": 0, "rate": 0.0, "hours": 0.0,
                "capped": False, "night": False, "max_hours": PASSIVE_INCOME_MAX_HOURS}
    # Копилка мелочи (carry) тоже участвует в выплате: /api/passive_income
    # выдаёт int(rate*hours + carry). Поэтому показываем ровно ту сумму, которую
    # игрок получит по кнопке, — иначе «Забрать 3» могло выдать 4.
    try:
        amount = float(pending.get("pending") or 0.0) + float(_passive_carry_get(user_id) or 0.0)
    except Exception:  # noqa: BLE001
        amount = float(pending.get("pending") or 0.0)
    return {
        "amount": round(amount, 3),
        "whole": int(amount),                 # сколько уже можно забрать
        "rate": pending.get("rate", 0.0),
        "hours": pending.get("hours", 0.0),
        "capped": pending.get("capped", False),
        "night": pending.get("night", False),
        "max_hours": pending.get("max_hours", PASSIVE_INCOME_MAX_HOURS),
    }


@app.post("/api/passive_preview")
async def passive_preview(auth: UserAuth):
    """Сколько накопил пассивный доход прямо сейчас (без начисления).

    Задача 56: клиент читает `amount`/`whole` (так же, как в /api/user →
    passive_pending), а этот ответ отдавал только `pending`. Из-за этого
    карточка в профиле всегда показывала «0 накоплено», а кнопка «Забрать»
    навсегда оставалась выключенной — пассивка выглядела сломанной.
    """
    uid = _auth_user_id(auth.initData)
    require_play_access_uid(uid)  # задача 57: техработы → только админы
    pending = await _passive_pending(uid)
    return {"ok": True, **pending,
            "amount": round(float(pending.get("pending") or 0.0), 3),
            "whole": int(float(pending.get("pending") or 0.0))}


PASSIVE_CARRY_TABLE = "passive_carry"


def _passive_carry_get(user_id) -> float:
    """Дробная «мелочь», которую ещё нельзя выдать целыми коинами (0..0.999…).

    Задача 42. Раньше при «Забрать» отметка сдвигалась на оплаченные часы, а
    дробные коины (при ставке 130.1/час это 0.1 каждый час) бесследно сгорали:
    игрок видел «накоплено 130.1», жал «Забрать», получал 130 — и 0.1 исчезала.
    Теперь мелочь хранится отдельно: доход пассивки в базе не трогаем, а
    `passive_income.last_at` двигаем только на ВЫПЛАЧЕННУЮ часть времени.
    """
    try:
        res = (supabase.table(PASSIVE_CARRY_TABLE).select("carry")
               .eq("user_id", int(user_id)).limit(1).execute())
        if res.data:
            return max(0.0, min(0.999999, float(res.data[0].get("carry") or 0)))
    except Exception:  # noqa: BLE001 — таблицы может не быть, это не ошибка
        pass
    return max(0.0, min(0.999999, float(_PASSIVE_CARRY_MEMORY.get(int(user_id), 0.0))))


_PASSIVE_CARRY_MEMORY: dict = {}


def _passive_carry_set(user_id, value: float) -> None:
    value = max(0.0, min(0.999999, float(value or 0)))
    _PASSIVE_CARRY_MEMORY[int(user_id)] = value
    if value <= 0:
        return
    try:
        supabase.table(PASSIVE_CARRY_TABLE).upsert(
            {"user_id": int(user_id), "carry": value}, on_conflict="user_id").execute()
    except Exception:  # noqa: BLE001 — без таблицы живём на памяти процесса
        pass


# Задача 56: один забор пассивки на игрока за раз. Раньше два быстрых нажатия
# «Забрать» (или повтор запроса при плохой сети) читали одну и ту же отметку
# last_at и начисляли коины ДВА раза — баланс рос из воздуха. Блокировка живёт
# в процессе (API — один инстанс), а внутри неё отметка перечитывается.
_PASSIVE_LOCKS: dict = {}


def _passive_lock(uid):
    lock = _PASSIVE_LOCKS.get(int(uid))
    if lock is None:
        lock = asyncio.Lock()
        _PASSIVE_LOCKS[int(uid)] = lock
    return lock


@app.post("/api/passive_income")
async def passive_income(data: PassiveIncomeRequest):
    """Пассивный доход с капсул: начисляется сервером по фактическому инвентарю.

    Формула: сумма(кол-во капсул * ставка в час) * прошедшие часы, но не больше
    24 часов за один заход. Начисление идемпотентно по времени: как только
    коины выданы, отметка last_at сдвигается ровно на «оплаченные» часы.
    Задача 56: параллельные запросы выполняются по очереди (см. _passive_lock).
    """
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: без начислений во время техработ
    async with _passive_lock(uid):
        # Работа с Supabase синхронная — уводим в поток, чтобы полторы-две
        # секунды запросов не замораживали весь API для остальных игроков.
        return await asyncio.to_thread(_passive_income_locked, data, uid)


def _passive_income_locked(data: PassiveIncomeRequest, uid):
    """Тело /api/passive_income под блокировкой: считает и выдаёт один раз."""
    row = _db_user(uid)
    if row is None:
        raise HTTPException(status_code=404, detail="Профиль не найден")
    inventory = parse_inventory(row.get("inventory"))
    rate, breakdown = _passive_rate_for(inventory)
    now = time.time()

    last = _passive_last_at(uid)
    degraded = last == "error"
    if degraded:
        # Таблицы нет И процесс ещё не видел начислений — считаем от метки в
        # памяти; если её тоже нет, начисление просто стартует сейчас.
        last = _PASSIVE_MEMORY.get(int(uid))
    if last is None:
        _passive_touch(uid, now)
        return {"ok": True, "accrued": 0, "rate": rate, "hours": 0.0, "real_hours": 0.0,
                "breakdown": breakdown, "new_balance": _safe_int(row.get("coins"), 0),
                "degraded": degraded,
                "night": _passive_multiplier_at(now) == PASSIVE_NIGHT_MULTIPLIER,
                "night_multiplier": PASSIVE_NIGHT_MULTIPLIER,
                "night_window": f"{PASSIVE_NIGHT_START_HOUR:02d}:00–"
                                f"{PASSIVE_NIGHT_END_HOUR:02d}:00 МСК",
                "detail": "Пассивный доход запущен — накопление идёт и при закрытой аппке"}

    # За один заход считаем не больше PASSIVE_INCOME_MAX_HOURS, но накопленное
    # НЕ сгорает: отметка сдвигается только на оплаченные часы, поэтому
    # следующий заход доберёт остаток. Пока Mini App закрыт — доход копится.
    window_end = min(now, float(last) + PASSIVE_INCOME_MAX_HOURS * 3600.0)
    real_hours, hours = _passive_split_hours(float(last), window_end)
    night_now = _passive_multiplier_at(now) == PASSIVE_NIGHT_MULTIPLIER
    carry = _passive_carry_get(uid)          # мелочь с прошлого раза
    entitled = rate * hours + carry          # и её тоже отдаём
    accrued = int(entitled)                  # дробные коины копим, а не теряем
    coins_now = _safe_int(row.get("coins"), 0)
    night_info = {"night": night_now, "night_multiplier": PASSIVE_NIGHT_MULTIPLIER,
                  "night_window": f"{PASSIVE_NIGHT_START_HOUR:02d}:00–"
                                  f"{PASSIVE_NIGHT_END_HOUR:02d}:00 МСК"}
    if accrued < 1 or rate <= 0:
        # Ничего не выдаём, отметку НЕ двигаем: копим дальше (в том числе мелочь).
        # Задача 44: отметку кладём в память, чтобы следующий заход (даже при
        # недоступной таблице) считал накопление от неё, а не начинал с нуля.
        _PASSIVE_MEMORY[int(uid)] = float(last)
        pending = max(0.0, entitled)
        return {"ok": True, "accrued": 0, "rate": rate, "hours": round(hours, 3),
                "real_hours": round(real_hours, 3),
                "entitled": round(pending, 3), "pending": round(pending, 3),
                "carry": round(pending - accrued, 3), "breakdown": breakdown,
                "new_balance": coins_now, "degraded": degraded, **night_info,
                "detail": ("Накопление идёт" if rate > 0
                           else "Нет капсул для пассивного дохода")}

    new_coins = award_coins(uid, accrued)
    if new_coins is None:
        raise HTTPException(status_code=503, detail="Не удалось начислить пассивный доход")
    # Задача 77: забранная пассивка — тоже операция в истории профиля.
    ledger_note(uid, "claim", accrued, "💤 Пассивный доход")
    # Сколько времени реально «съели» выданные коины, и что осталось в мелочи.
    # consumed_hours = (accrued - carry) / rate — время, за которое набежали
    # именно выданные коины; всё, что не влезло в целые, уходит в carry.
    consumed_hours = max(0.0, (accrued - carry) / rate) if rate > 0 else 0.0
    consumed_hours = min(consumed_hours, hours)
    leftover = max(0.0, entitled - accrued)          # 0..0.999…
    _passive_carry_set(uid, leftover)
    fraction = min(1.0, (consumed_hours / hours) if hours > 0 else 1.0)
    _passive_touch(uid, min(window_end, float(last) + real_hours * 3600.0 * fraction))
    return {"ok": True, "accrued": accrued, "rate": rate, "hours": round(hours, 3),
            "real_hours": round(real_hours, 3),
            "breakdown": breakdown, "new_balance": new_coins, "degraded": degraded,
            "carry": round(leftover, 3),
            "pending": round(max(0.0, entitled - accrued), 3),
            **night_info,
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
    # Задача 43: «Заработано» — все начисления по рефералам (в т.ч. ожидающие
    # подтверждения), иначе цифра оставалась нулём даже после прихода друга.
    earned = sum(_safe_int(row.get("reward"), 0) for row in rows)
    paid = sum(_safe_int(row.get("reward"), 0) for row in rows
               if (row.get("status") or "") == "paid")
    return {
        "available": True,
        "count": len(rows),
        "paid_count": sum(1 for row in rows if (row.get("status") or "") == "paid"),
        "pending": max(0, len(rows) - sum(1 for row in rows
                                           if (row.get("status") or "") == "paid")),
        "earned": earned,
        "earned_paid": paid,
        # Сколько дают за друга — те же числа, что платит сервер (никаких
        # захардкоженных «от 500 до 2000» в интерфейсе).
        "reward_rules": {"base": REFERRAL_REWARD_BASE, "nft2": REFERRAL_REWARD_NFT2,
                         "premium": REFERRAL_REWARD_PREMIUM},
        "invited": [{"user_id": row.get("referred_id"),
                     "reward": _safe_int(row.get("reward"), 0),
                     "status": row.get("status") or "pending"} for row in rows[:20]],
        # Шаблон оставлен для старого клиента: /api/referrals подменяет его
        # готовой ссылкой на настоящего бота.
        "link": f"https://t.me/{{bot}}?startapp=ref_{int(user_id)}",
    }


@app.post("/api/referral_bind")
async def referral_bind(data: ReferralBindRequest):
    """Привязать пригласившего. Работает один раз: у referred_id уникальный ключ."""
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: техработы → только админы
    referrer_id = _referral_parse_code(data.ref_code)
    if referrer_id is None:
        return {"ok": False, "detail": "Некорректный код приглашения"}
    if referrer_id == uid:
        return {"ok": False, "detail": "Нельзя пригласить самого себя"}
    if _db_user(referrer_id) is None:
        return {"ok": False, "detail": "Пригласивший не найден"}
    try:
        res = (supabase.table(REFERRALS_TABLE).select("referred_id, referrer_id")
               .eq("referred_id", uid).limit(1).execute())
        if res.data:
            return {"ok": True, "already": True, "detail": "Приглашение уже учтено"}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=(
            "Таблица referrals не создана. Примените миграцию "
            "supabase/migrations/20261109000000_economy_social.sql")) from exc
    # Задача 60: не привязываем «по кругу» — если пригласивший сам пришёл по
    # ссылке этого игрока, получается вечный обмен наградами. Проверяем один
    # запрос в обе стороны.
    try:
        back = (supabase.table(REFERRALS_TABLE).select("referred_id")
                .eq("referred_id", referrer_id).eq("referrer_id", uid).limit(1).execute())
        if back.data:
            return {"ok": False, "detail": "Такое приглашение уже было — второй раз не считаем"}
    except Exception:  # noqa: BLE001
        pass

    # Задача 60: строку пишем «upsert-ом» по первичному ключу referred_id.
    # Раньше insert падал на дубле (23505), когда игрок открывал ссылку дважды
    # в разных сессиях, и приглашение считалось неудачным — оно уже было, но
    # клиент это не различал.
    try:
        supabase.table(REFERRALS_TABLE).upsert({
            "referred_id": uid, "referrer_id": referrer_id, "reward": 0,
            "status": "pending", "created_at": _now_iso(),
        }, on_conflict="referred_id").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert реферала не удался (%s) — пробую insert", exc)
        try:
            _insert_tolerant(REFERRALS_TABLE, {
                "referred_id": uid, "referrer_id": referrer_id, "reward": 0,
                "status": "pending", "created_at": _now_iso(),
            }, optional=("created_at", "paid_at"), logger_prefix="РЕФЕРАЛ: ")
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Не удалось записать реферала %s → %s: %s", referrer_id, uid, exc2)
            return {"ok": False, "detail": "Не удалось сохранить приглашение"}
    return {"ok": True, "referrer_id": referrer_id, "status": "pending"}


@app.post("/api/referral_check")
async def referral_check(data: UserAuth):
    """Проверка статуса реферала при его входе в Mini App.

    Награда достаётся пригласившему и начисляется один раз: статус в таблице
    переводится из pending в paid, повторный вызов ничего не платит.
    """
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: бонусы не начисляем во время техработ
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

    # Задача 37: награда не должна ломать вход приглашённому. Раньше, если у
    # пригласившего не было строки в users (или запись не прошла), игрок получал
    # 503 — и приглашение навсегда оставалось pending. Теперь просто отвечаем
    # «пока не начислено» и пробуем снова при следующем заходе.
    new_balance = award_coins(referrer_id, reward)
    if new_balance is None:
        logger.warning("Реферал %s → %s: награда %s не начислилась, попробуем позже",
                       referrer_id, uid, reward)
        return {"ok": True, "available": True, "referred": True, "paid": False,
                "pending": True, "reward": reward,
                "detail": "Награда пригласившему начислится чуть позже"}
    try:
        supabase.table(REFERRALS_TABLE).update({
            "reward": reward, "status": "paid", "paid_at": _now_iso(),
        }).eq("referred_id", uid).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось отметить реферальную награду: %s", exc)

    # Пригласивший узнаёт о награде в личке бота, а не «где-то в приложении».
    await _send_user_dm(
        referrer_id,
        f"🎉 <b>По твоей ссылке пришёл игрок!</b>\n\n"
        f"Начислено: <b>+{reward} коинов</b>\n"
        f"Баланс: <b>{new_balance}</b>\n\n"
        f"Зови ещё — за каждого друга платим отдельно.",
    )
    return {"ok": True, "available": True, "referred": True, "paid": True,
            "reward": reward, "referrer_balance": new_balance,
            "detail": f"Пригласивший получил {reward} коинов"}


async def _resolve_bot_username() -> str:
    """@username бота, который реально ведёт игру (для ссылок на Mini App).

    Порядок: явный env → Telegram getMe по BOT_TOKEN → константа. Результат
    кешируем на час: getMe не нужен на каждый запрос, а после переименования
    бота ссылка сама станет правильной.
    """
    if BOT_USERNAME_OVERRIDE:
        return BOT_USERNAME_OVERRIDE
    cached = _BOT_USERNAME_CACHE.get("value") or ""
    if cached and time.time() - float(_BOT_USERNAME_CACHE.get("at") or 0) < 3600:
        return cached
    if BOT_TOKEN:
        try:
            payload = await _http_json(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe",
                                       timeout=6)
            username = str(((payload or {}).get("result") or {}).get("username") or "")
            if username:
                _BOT_USERNAME_CACHE.update({"value": username, "at": time.time()})
                return username
        except Exception as exc:  # noqa: BLE001
            logger.warning("getMe недоступен для реферальной ссылки: %s", exc)
    return cached or DEFAULT_BOT_USERNAME


def _referral_link(bot_username: str, user_id) -> str:
    """Ссылка-приглашение на конкретного бота: t.me/<бот>?startapp=ref_<id>."""
    username = (bot_username or DEFAULT_BOT_USERNAME).lstrip("@")
    return f"https://t.me/{username}?startapp=ref_{int(user_id)}"


@app.post("/api/referrals")
async def referrals_info(data: UserAuth):
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: техработы → только админы
    stats = _referral_stats(uid)
    bot_username = await _resolve_bot_username()
    stats.update({"ok": True, "code": f"ref_{uid}", "bot_username": bot_username,
                  # Ссылку отдаём готовой: клиент больше ничего не подставляет.
                  "link": _referral_link(bot_username, uid)})
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
    require_play_access_uid(uid)  # задача 57: покупки закрыты во время техработ
    package = next((p for p in STARS_PACKAGES if p["id"] == data.package_id), None)
    if not STARS_ENABLED:
        return {"ok": False, "enabled": False, "packages": STARS_PACKAGES,
                "detail": "Покупка коинов за Stars скоро появится"}
    if package is None:
        raise HTTPException(status_code=400, detail="Неизвестный пакет коинов")
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink"
    total = stars_package_total(package)
    title = f"{package['stars']} ⭐ → {total} коинов"
    description = f"{package['coins']} коинов"
    if package.get("bonus"):
        description += f" + {package['bonus']} бонус за крупный донат"
    description += " в AURA GAME"
    payload = {
        "title": title,
        "description": description,
        # Префикс «stars» — новый прайс; старые инвойсы с «coins:» тоже
        # принимаются (stars_package_by_payload), чтобы никто не потерял оплату.
        "payload": f"stars:{package['id']}:{uid}",
        "currency": "XTR",
        # Для Stars цена передаётся в самом «звёздном» номинале, без *100.
        "prices": [{"label": f"{total} коинов", "amount": int(package["stars"])}],
    }
    try:
        result = await _http_json(url, method="POST", payload=payload, timeout=10)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="Telegram недоступен, попробуйте позже") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        detail = (result or {}).get("description") if isinstance(result, dict) else ""
        raise HTTPException(status_code=503, detail=detail or "Не удалось создать счёт")
    return {"ok": True, "enabled": True, "url": result.get("result"),
            "coins": package["coins"], "bonus": package.get("bonus", 0),
            "total": total, "stars": package["stars"]}


@app.get("/api/stars_packages")
async def stars_packages():
    # Витрина: у каждого пакета есть total с бонусом — клиент подсвечивает
    # радужным именно бонусные пакеты (задача 70).
    packages = [{**p, "total": stars_package_total(p)} for p in STARS_PACKAGES]
    return {"enabled": STARS_ENABLED, "packages": packages, "currency": "XTR"}


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
        _insert_tolerant(STARS_PAYMENTS_TABLE, {
            "charge_id": str(charge_id), "user_id": uid, "coins": int(coins),
            "stars": int(stars), "created_at": _now_iso(),
        }, logger_prefix="STARS: ")
    except Exception as exc:  # noqa: BLE001
        logger.warning("stars_payments недоступна: %s", exc)
        return None
    return award_coins(uid, int(coins))


def _stars_periods(rows) -> dict:
    """Покупки звёзд по периодам: сколько раз, сколько звёзд и коинов (задача 70)."""
    now = time.time()
    periods = {"24h": now - 86400, "week": now - 7 * 86400, "month": now - 30 * 86400}
    result = {name: {"count": 0, "stars": 0, "coins": 0} for name in ("24h", "week", "month", "all")}
    for row in rows or []:
        created = _parse_timestamp(row.get("created_at"))
        stamp = created if created is not None else 0
        stars = _safe_int(row.get("stars"), 0)
        coins = _safe_int(row.get("coins"), 0)
        for name, edge in periods.items():
            if stamp >= edge:
                result[name]["count"] += 1
                result[name]["stars"] += stars
                result[name]["coins"] += coins
        result["all"]["count"] += 1
        result["all"]["stars"] += stars
        result["all"]["coins"] += coins
    return result


def stars_stats_sync() -> dict:
    """Статистика донатов: по периодам + сколько всего (для админки и `стата бота`)."""
    try:
        rows = (supabase.table(STARS_PAYMENTS_TABLE)
                .select("stars, coins, created_at").execute().data or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("stars_payments недоступна для статистики: %s", exc)
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "periods": _stars_periods(rows), "rows": len(rows)}


@app.post("/api/stars_stats")
async def stars_stats(data: UserAuth):
    """Статистика покупок за звёзды — только админам."""
    _require_admin_api(data.initData)
    return await asyncio.to_thread(stars_stats_sync)


# ---------- Админ-API (то же, что показывает бот) ----------
@app.post("/api/admin/settings")
async def admin_settings(data: UserAuth):
    _require_admin_api(data.initData)
    values = _load_game_settings(force=True)
    return {
        "ok": True,
        "settings": values,
        "effective": {
            # Апгрейд не настраивается: 90 / X — жёстко. Поля оставлены, чтобы
            # старый клиент/админка не падали.
            "upgrade_chance_factor": 1.0,
            "upgrade_house_edge": 1.0,
            "crash_house_edge": game_setting_float("crash_house_edge", 1.0),
        },
        "admin_only_app": app_closed_now(),
    }


@app.post("/api/admin/set_setting")
async def admin_set_setting(data: AdminSettingRequest):
    _require_admin_api(data.initData)
    key = str(data.key or "").strip()
    ok = game_setting_set(key, data.value)
    if not ok:
        raise HTTPException(status_code=503, detail="Не удалось сохранить настройку")
    touched = {}
    # Задача 76: кулдаун РП не настраивается (5 часов, один на игрока), поэтому
    # старые ключи `love_cooldown_*` просто игнорируются — чистить их не нужно.
    if key in ("maintenance", MAINTENANCE_SETTING_KEY):
        # Задача 71: тумблер техработ. Принимаем "1"/"0", "on"/"off", true/false.
        raw = data.value
        enabled = (raw is True or str(raw).strip().lower()
                   in ("1", "true", "yes", "on", "вкл", "включить"))
        if not set_maintenance(enabled):
            raise HTTPException(status_code=503, detail="Не удалось сохранить настройку")
        touched = {"maintenance": maintenance_now(),
                   "admin_only_app": app_closed_now(),
                   "maintenance_message": MAINTENANCE_MESSAGE}
    return {"ok": True, "key": key, "value": data.value, **touched}


@app.post("/api/admin/maintenance")
async def admin_maintenance(data: UserAuth):
    """Тумблер техработ: `{"enabled": true}` включает, `false` — выключает.

    Читать состояние можно и без этого вызова: `/api/user` отдаёт `maintenance`
    в профиле, а админ всегда может войти и увидеть панель.
    """
    _require_admin_api(data.initData)
    return {"ok": True, "maintenance": maintenance_now(),
            "message": MAINTENANCE_MESSAGE}


@app.post("/api/admin/set_maintenance")
async def admin_set_maintenance(data: AdminMaintenanceRequest):
    """Включить/выключить техработы. Доступно только ADMIN_IDS."""
    admin_id = _require_admin_api(data.initData)
    if not set_maintenance(bool(data.enabled)):
        raise HTTPException(status_code=503, detail="Не удалось сохранить настройку")
    logger.warning("Техработы %s (админ %s)",
                   "ВКЛЮЧЕНЫ" if data.enabled else "выключены", admin_id)
    return {"ok": True, "maintenance": maintenance_now(),
            "enabled": bool(data.enabled), "message": MAINTENANCE_MESSAGE}


@app.post("/api/admin/game_settings")
async def admin_game_settings(data: UserAuth):
    """Что сейчас в настройках, которые читает игра (нужно админ-панели)."""
    _require_admin_api(data.initData)
    return {
        "ok": True,
        "maintenance": maintenance_now(),
        "maintenance_message": MAINTENANCE_MESSAGE,
        # Задача 76: значение фиксированное, из базы не читается.
        "love_cooldown_seconds": love_cooldown_seconds(),
        "love_cooldown_note": "5 часов, общий для всех РП, не настраивается",
    }


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
    # Задача 47: даже проверка базы — блокирующий вызов. Уводим в поток, чтобы
    # не подвешивать event loop (из-за этого «сбрасывалась» и арена).
    return await asyncio.to_thread(_admin_stats_sync, data.initData)


def _admin_stats_sync(init_data: str):
    _require_admin_api(init_data)
    stats = {"users": 0, "coins_in_system": 0, "upgrade_bets": 0, "upgrade_paid": 0,
             "upgrade_spins": 0, "crash_rounds": 0, "settings": _load_game_settings(force=True)}
    # Задача 47: видно, применима ли миграция арены (журнал выплат и снимки
    # раундов). Без них игра работает, но выплата не переживёт перезапуск.
    stats["arena_payouts"] = bool(_arena_payout_backend()["ledger"])
    stats["arena_payout_rpc"] = bool(_arena_payout_backend()["rpc"])
    stats["arena_rounds"] = _arena_rounds_available()
    stats["arena_pending_payouts"] = len(PENDING_PAYOUTS)
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


def _love_days(row) -> int:
    """Сколько дней пара вместе (день создания = 1-й день)."""
    raw = (row or {}).get("created_at")
    if not raw:
        return 0
    try:
        stamp = str(raw).replace("Z", "+00:00")
        parsed = datetime.datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return max(0, int((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds() // 86400))
    except Exception:
        return 0


def _clean_person_name(value) -> str:
    """Имя для плашки: без «@», без HTML-углов и без пустоты."""
    return str(value or "").strip().lstrip("@").replace("<", "").replace(">", "")[:64]


def _love_partner_name(partner_id, row=None) -> str:
    """Как назвать партнёра в Mini App — только человеческое имя.

    Задача 55: раньше брался `users.username`, и в плашке появлялся чужой/
    протухший ник («Contact_blocked») — игрок не понимал, с кем он в паре.
    Теперь имя берём из `first_name`/`name`, а username
    не показываем вовсе (задача 44: @-ников в текстах бота нет).

    Задача 60: `id<число>` в плашке игрока не устраивает («В отношениях с
    id1615872008»). Поэтому если в базе имени нет — спрашиваем Telegram
    (`getChat`, только кэш на 10 минут, ошибки глотаем): бот и так знает
    человека, с которым игрок в паре. И только если Telegram тоже не ответил,
    остаётся нейтральное «партнёр» — без цифр.
    """
    if not partner_id:
        return "партнёр"
    row = row or _db_user(partner_id)
    name = (_clean_person_name((row or {}).get("first_name"))
            or _clean_person_name((row or {}).get("name")))
    if name:
        return name
    live = _telegram_person_name(partner_id)
    if live:
        return live
    return "партнёр"


# Имя партнёра из Telegram: getChat дешевле, чем хранить имена всех игроков, и
# одно и то же значение не запрашивается чаще раза в 10 минут на человека.
_PARTNER_NAME_CACHE: dict = {}
_PARTNER_NAME_TTL = 600


def _telegram_person_name(user_id) -> str:
    """Синхронный getChat → имя человека ('' если не получилось).

    `love_state_for` синхронный, поэтому ходим в Telegram обычным `urllib` с
    таймаутом 3 секунды: имя партнёра не должно задерживать вход в профиль.
    Ошибки сети и «chat not found» гасим — плашка просто останется без имени.
    """
    uid = _safe_int(user_id, None)
    if uid is None or not BOT_TOKEN:
        return ""
    now = time.time()
    cached = _PARTNER_NAME_CACHE.get(uid)
    if cached and now - cached[0] < _PARTNER_NAME_TTL:
        return cached[1]
    name = ""
    payload = None
    try:
        # Обычный urllib, а не httpx-хелпер: функция синхронная и вызывается из
        # потока, а внутри уже работающего event loop `asyncio.run` упал бы.
        url = (f"https://api.telegram.org/bot{BOT_TOKEN}/getChat?"
               + urllib.parse.urlencode({"chat_id": uid}))
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 — нет сети/бота в чате: не критично
        logger.warning("getChat для имени партнёра %s не сработал: %s", uid, exc)
        payload = None
    person = ((payload or {}).get("result") or {}) if isinstance(payload, dict) else {}
    if isinstance(person, dict):
        name = (_clean_person_name(person.get("first_name"))
                or _clean_person_name(person.get("full_name"))
                or _clean_person_name(person.get("username"))
                or _clean_person_name(person.get("title")))
    _PARTNER_NAME_CACHE[uid] = (now, name)
    return name


def _love_solo_for(user_id) -> dict:
    """Одиночная ветка (Unlove): счёт и уровень без наград."""
    try:
        raw = love_solo_state(supabase, user_id) or {}
    except Exception:
        raw = {}
    score = _safe_int(raw.get("love"), 0)
    level = love_public_stage(score)
    return {
        "solo": True,
        "unlove": score,
        "solo_currency": SOLO_CURRENCY,
        "solo_level": level["level"],
        "solo_title": level["title"],
        "solo_progress": level["progress"],
        "solo_days": _love_days({"created_at": raw.get("created_at")}),
    }


# Кулдаун награды за РП (задача 76): ОДИН на игрока и общий для всех команд —
# 5 часов, не настраивается. Раньше значение читалось из game_settings и могло
# быть своим у каждой команды; команда `кд` удалена, поэтому строки в базе
# (`love_cooldown_seconds`, `love_cooldown_actions`) больше не читаются.
def _love_cooldowns() -> dict:
    """Совместимость с ответом Mini App: только общее значение."""
    return {"*all": love_cooldown_seconds()}


def _love_cooldown_seconds(action=None) -> int:
    """Кулдаун РП в секундах. От команды не зависит (задача 76)."""
    return love_cooldown_seconds(action)


def love_state_for(user_id) -> dict:
    """Состояние отношений: партнёр, Love коины и текущий уровень.

    Этапы и награды за них — СЕКРЕТНАЯ механика. Клиенту уходят только уровень,
    Love и прогресс: никаких сумм наград и «следующего этапа», иначе игроки
    считают таблицу выплат вместо того, чтобы играть.

    Задача 30: если пары нет, отдаём одиночную ветку (Unlove) — она копится у
    одного человека и сгорает при вступлении в отношения.
    """
    row = _love_relationship_for(user_id)
    if not row:
        return {"ok": True, "in_relationship": False, "available": True,
                "stages": love_public_stages(), "action_bonus": LOVE_ACTION_BONUS,
                # Задача 76: кулдаун один на игрока (5 часов) и общий для всех
                # команд — карта по действиям больше не нужна.
                "cooldown_hours": round(_love_cooldown_seconds() / 3600, 1),
                "cooldown_seconds": _love_cooldown_seconds(),
                # Задача 76: персональных кулдаунов нет — поле оставлено пустым
                # для старых сборок Mini App.
                "cooldowns": {},
                "ice_cost": LOVE_ICE_COST, "rpc_actions": list(RP_ACTIONS),
                **_love_solo_for(user_id)}
    score = _safe_int(row.get("love"), 0)
    partner_id = _love_partner_id(row, user_id)
    level = love_public_stage(score)
    return {
        "ok": True,
        "available": True,
        "in_relationship": True,
        "partner_id": partner_id,
        "partner_name": _love_partner_name(partner_id, row),
        "love": score,
        "bank": _safe_int(row.get("bank"), 0),
        "days": _love_days(row),
        # Совместимость: stage.title/level — но без reward и без next_stage.
        "stage": level,
        "level": level["level"],
        "stage_title": level["title"],
        "progress": level["progress"],
        "action_bonus": LOVE_ACTION_BONUS,
        # Задача 72: готовый текст прогресса — клиент не считает его сам.
        "progress_text": action_progress_text(score),
        # Задача 61: своё значение у каждой команды + общее (cooldown_*).
        "cooldown_hours": round(_love_cooldown_seconds() / 3600, 1),
        "cooldown_seconds": _love_cooldown_seconds(),
        # Задача 76: персональных кулдаунов нет (КД общий на игрока).
        "cooldowns": {},
        "ice_cost": LOVE_ICE_COST,
        "rpc_actions": list(RP_ACTIONS),
    }


@app.post("/api/love_state")
async def love_state(data: UserAuth):
    """Плашка «В отношениях» и прогресс этапа для профиля Mini App."""
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)  # задача 57: награды этапов — во время техработ никому
    state = love_state_for(uid)
    if not state.get("in_relationship"):
        # Одиночка: страховка как у пар — если этап перешагнули, а бот не успел
        # начислить (действие без интернета), награждаем здесь. Размер награды
        # и название этапа наружу не уходят, только флаг «стало теплее».
        score = _safe_int(state.get("unlove"), 0)
        if score > 0:
            try:
                granted = claim_solo_stage_rewards(supabase, uid, score)
            except Exception:
                granted = []
            if granted:
                state["stage_up"] = True
                # Задача 74: призы за этапы отношений — тоже «заработано».
                reward_total = sum(int(g.get("reward") or 0) for g in granted)
                stats_note(uid, coins_earned=reward_total, won_love=reward_total)
                ledger_note(uid, "reward", reward_total, "💗 Награда за уровень")
                await _love_stage_dm({"user_a": uid}, granted)
        return state
    # Пара: та же страховка через общую таблицу наград.
    row = _love_relationship_for(uid)
    if row:
        granted = claim_stage_rewards(supabase, row)
        # Задача 61: клиенту по-прежнему уходят только название уровня и флаг
        # «стало теплее» — таблицу призов заранее не показываем. А вот КАЖДОМУ
        # участнику пары о новом уровне сообщаем в личку: награда начислена,
        # и он должен знать, за что пришли коины.
        if granted:
            state["stage_up"] = True
            # Задача 74: каждому партнёру — своя сумма приза в статистику.
            for partner_id in (_safe_int(row.get("user_a"), 0), _safe_int(row.get("user_b"), 0)):
                if partner_id > 0:
                    reward_total = sum(int(g.get("reward") or 0) for g in granted)
                    stats_note(partner_id, coins_earned=reward_total, won_love=reward_total)
                    ledger_note(partner_id, "reward", reward_total, "💗 Награда за уровень")
            await _love_stage_dm(row, granted)
    return state


async def _love_stage_dm(row, granted) -> None:
    """Личное сообщение каждому партнёру: «вы достигли N уровня, получаете X».

    Вызывается и из Mini App (`/api/love_state`), и из бота — но в базе
    love_stage_rewards этап отмечается до начисления, поэтому «granted»
    приходит только один раз, и дублей сообщений не будет.
    """
    if not row or not granted:
        return
    partners = []
    for key in ("user_a", "user_b"):
        uid = _safe_int(row.get(key), 0)
        if uid > 0 and uid not in partners:
            partners.append(uid)
    for uid in partners:
        lines = ["💗 <b>Новый этап отношений!</b>", ""]
        for stage in granted:
            lines.append(stage_reached_text(stage, stage.get("reward")))
        lines += ["", "Коины уже начислены на баланс — забирать ничего не нужно.",
                  "Прогресс: «любовь инфа» в боте."]
        await _send_user_dm(uid, "\n".join(lines))

# ==================== ИГРОВЫЕ МОМЕНТЫ ПРОФИЛЯ (задача 74) ====================
# Заказчик: «профиль — это профиль». В профиле нужны маленькие факты про
# игрока, а не про вещи (вещи — в «Кармане»). Всё, что не накопилось, отдаём
# нулём и флагом ready=False — клиент покажет «—», а не выдуманное число.


def _profile_stats_record(user_id, stat_row: dict, first_seen) -> dict:
    """«Достижения» профиля: ровно то, что просил заказчик (задача 77).

    Было 13 строк про деньги и рекорды — заказчик оставил четыре:
      • сколько потрачено коинов за всё время,
      • сколько сделано апгрейдов,
      • сколько открыто сундуков,
      • сколько сыграно каток в мины.
    Всё остальное живёт в других карточках профиля и в «Кармане».
    """

    def num(key):
        return _safe_int(stat_row.get(key), 0)

    rows = [
        {"key": "spent", "icon": "💸", "label": "Потрачено коинов",
         "value": num("coins_spent"), "money": True,
         "hint": (f"Апгрейды {num('spent_upgrade')} · магазин {num('spent_shop')} · "
                  f"ракета {num('spent_crash')} · арена {num('spent_arena')} · "
                  f"мины {num('spent_mines')}") if num("coins_spent") else "Пока ничего не потрачено"},
        {"key": "upgrades", "icon": "🔁", "label": "Апгрейдов сделано",
         "value": num("upgrades_total"), "count": True,
         "hint": f"Удачных: {num('upgrades_won')}"},
        {"key": "chests", "icon": "📦", "label": "Сундуков открыто",
         "value": num("chests_opened"), "count": True,
         "hint": f"Золотых: {num('golden_chests')}"},
        {"key": "mines", "icon": "💣", "label": "Каток в мины",
         "value": num("mines_games"), "count": True,
         "hint": f"Выигрышей: {num('won_mines')}"},
    ]
    for row in rows:
        value = num_of(row.get("value"))
        row["value"] = value
        if row.get("money"):
            row["value_text"] = _coins_text(value)
        else:
            row["value_text"] = str(value) if value else "—"
    return {
        "ok": True,
        # ready=False — таблицы ещё нет (миграция не применена): клиент честно
        # пишет «Статистика копится», а не рисует нули как достижения.
        "ready": bool(stat_row),
        "days": _days_since(first_seen) if first_seen else 0,
        "since": _iso_from_timestamp(first_seen) if first_seen else None,
        "rows": rows,
    }


def num_of(value) -> int:
    """int без исключений: None/''/мусор → 0 (статистика не должна падать)."""
    return _safe_int(value, 0)


def _coins_text(value: int) -> str:
    """Коины для профиля: «1 234 567». Пустое значение — «—», а не «0»."""
    amount = num_of(value)
    if amount <= 0:
        return "—"
    return f"{amount:,}".replace(",", " ")


@app.post("/api/profile_stats")
async def profile_stats(data: UserAuth):
    """«Игровые моменты» для экрана профиля (задача 74).

    Отдаём сразу готовые строки: клиент не считает деньги и не знает, где что
    лежит в базе. Нет миграции — строки приходят нулевыми с ready=False,
    профиль при этом открывается как обычно.
    """
    uid = _auth_user_id(data.initData)
    require_play_access_uid(uid)
    stat_row = await asyncio.to_thread(player_stats.read_stats, supabase, uid)
    first_seen = _app_first_seen_get(uid)
    payload = _profile_stats_record(uid, stat_row or {}, first_seen)
    payload["history"] = _profile_history(uid)
    payload["migration"] = player_stats.MIGRATION_FILE
    return payload


def _profile_history(user_id, limit: int = 30) -> list:
    """История ставок и пополнений «всё в одном» (задача 77).

    Одна строка = одна операция по кошельку. Знак и цвет считает клиент по
    `sign`: минус — ставка/покупка/перевод другу, плюс — выигрыш/награда.
    """
    rows = player_stats.read_ledger(supabase, user_id, limit)
    out = []
    for row in rows:
        kind = str(row.get("kind") or "")
        amount = _safe_int(row.get("amount"), 0)
        created = row.get("created_at")
        out.append({
            "id": _safe_int(row.get("id"), None),
            "kind": kind,
            "label": player_stats.LEDGER_LABELS.get(kind, "Операция"),
            "title": row.get("title") or player_stats.LEDGER_LABELS.get(kind, ""),
            "amount": amount,
            "amount_text": _coins_text(amount),
            "sign": "-" if kind in ("bet", "buy", "transfer_out") else "+",
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        })
    return out


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


@app.api_route("/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
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
        return _miniapp_page()
    if name in ROOT_STATIC_FILES or os.path.isfile(os.path.join(MINIAPP_DIR, name)):
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