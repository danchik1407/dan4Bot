# Деплой AURA GAME: ровно два сервиса Render

Проект обслуживают **два** Web Service в Render. Третий (`dan4Bot-1`) удалён —
в коде нет ссылок на другие домены, а вся логика разложена так:

| Сервис | Что запускает | Чем занимается |
|---|---|---|
| **aura-api** | `uvicorn api:app --host 0.0.0.0 --port $PORT` | FastAPI: все игры, экономика, сундуки, магазин, апгрейд **и раздача Mini App** (`/miniapp` → `index.html`, `app.js`, `style.css`, картинки) |
| **dan4Bot** | `python bot.py` | aiogram (long polling): команды бота. Веб-сервер поднимается только формально (`start_fake_website`), чтобы Render видел открытый порт |

Оба сервиса используют **одну и ту же базу Supabase**, поэтому баланс в боте и в
Mini App — это одна запись (`users.coins`), синхронизация не нужна.

---

## 1. aura-api (Web Service)

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn api:app --host 0.0.0.0 --port $PORT`
- **Health Check Path:** `/api/health`
- **Environment:**
  - `SUPABASE_URL`
  - `SUPABASE_KEY`
  - `BOT_TOKEN`
  - `MAINTENANCE_MODE=0` — выключить техработы (по умолчанию **включены**:
    играют только `ADMIN_IDS`, остальные видят экран «Технические работы»)
- **Адреса:**
  - Mini App: `https://aura-api-mptt.onrender.com/miniapp/`
  - Корень `/` делает 307-редирект на `/miniapp/`
  - API: `https://aura-api-mptt.onrender.com/api/...`

Статика монтируется в `api.py`:

```python
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MINIAPP_DIR = os.path.join(BASE_DIR, "miniapp")
app.mount("/miniapp", StaticFiles(directory=MINIAPP_DIR, html=True), name="miniapp")
```

CORS открыт (`allow_origins=["*"]`): в WebView Telegram Origin бывает
`https://web.telegram.org`, `t.me` или отсутствует. Куки не используются —
авторизация только через подписанный `initData`.

## 2. dan4Bot (Web Service)

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python bot.py`
- **Environment:**
  - `BOT_TOKEN`
  - `SUPABASE_URL`
  - `SUPABASE_KEY`
  - (необязательно) `AURA_API_URL` — по умолчанию `https://aura-api-mptt.onrender.com`
  - (необязательно) `AURA_WEBAPP_URL` — по умолчанию `AURA_API_URL + "/miniapp/"`
  - (необязательно) `MINIAPP_SHORT_NAME` — короткое имя из BotFather (по умолчанию `Auragame`)
  - (необязательно) `MINIAPP_URL` — если нужно жёстко задать прямую ссылку `t.me/<бот>/<short_name>`
  - (необязательно) `BOT_USERNAME` — имя бота без `@`; по умолчанию берётся у Telegram при старте
  - Render сам добавляет `PORT` — его слушает `start_fake_website`

## 3. BotFather

`Configure Mini App` → **Web App URL** должен быть
`https://aura-api-mptt.onrender.com/miniapp/` (то есть `WEBAPP_URL` из `bot.py`).
`short_name` (`Auragame`) даёт прямую ссылку `https://t.me/<ваш_бот>/Auragame`.

**Как бот формирует ссылку в `!апка`:**

1. В личке — кнопка `web_app` с адресом `WEBAPP_URL` (это Mini App на aura-api).
   Telegram открывает приложение сам и передаёт `initData`, короткое имя тут не
   участвует — поэтому кнопка работает даже если `short_name` в BotFather другой.
2. Вторая кнопка — прямая ссылка `t.me/<бот>/<short_name>` для пересылки другу.
   `<бот>` подставляется из `get_me()` при старте, а не из константы в коде:
   после переименования бота ссылка не ломается.
3. В группе (Telegram запрещает там `web_app`) остаётся только эта прямая ссылка.

Если в BotFather `short_name` не `Auragame` — задайте `MINIAPP_SHORT_NAME` в
сервисе dan4Bot или `MINIAPP_URL` целиком; перезапуск бота применит значение.
Имя бота обязано совпадать с реальным `@username` — иначе `t.me` покажет пустую
страницу (про это в логах: `🔗 Прямая ссылка Mini App: …`).

## 3.1. Если видите «Not Found»

`Not Found` — это ответ 404. Ниже все места, где он раньше появлялся, и что теперь:

| Что было не так | Было | Стало |
|---|---|---|
| В BotFather указан корень сервиса (без `/miniapp/`) | `Not Found` | 307 → `/miniapp/` |
| В BotFather указан `/index.html`, `/app.js`, `/style.css` | `Not Found` | 307 → `/miniapp/<файл>` |
| В BotFather указан адрес сервиса **dan4Bot** | `Not Found` | редирект в Mini App на aura-api |
| Имя Web Service в Render ≠ адреса в коде (`aura-api-mppt` / `aura-api-mptt`) | Render отвечает 404 на весь домен | клиент сам находит живой `/api/health`, бот на старте пишет рабочий адрес в лог |
| Запрос к `/api/...` несуществующего сервиса | `{"detail":"Not Found"}` | то же, но в `!сервисы` видно «❌ 404 Not Found» и подсказка про `AURA_API_URL` |
| Открыли `/webapp`, `/main` и т.п. | `Not Found` | 404 с подсказкой: где Mini App, где `/api/health`, где `/api/docs` |

Проверка адресов (в браузере или терминале):

```bash
curl -s https://<сервис>.onrender.com/api/health        # {"ok":true,"service":"aura-api",...}
curl -sI https://<сервис>.onrender.com/                 # 307 → /miniapp/
curl -sI https://<сервис>.onrender.com/index.html       # 307 → /miniapp/index.html
```

В боте (админ): `!сервисы` — покажет `✅ онлайн` или `❌ 404 Not Found` с причиной.
Если нужного домена нет в коде — задайте переменную `AURA_API_URL` в сервисе dan4Bot,
перезапуск не требуется для клиента: он всё равно возьмёт origin страницы `/miniapp/`.

## 4. Как проверить, что всё на двух сервисах

1. В боте (админ): `!сервисы` — покажет статус `aura-api` (`/api/health`),
   адрес API, адрес Mini App и подтверждение, что второй сервис — сам бот.
2. В браузере: `https://aura-api-mptt.onrender.com/api/health` →
   `{"ok": true, "service": "aura-api", "static": {"ready": true, ...}}`.
3. `https://aura-api-mptt.onrender.com/miniapp/` — открывается Mini App
   (без Telegram покажет экран техработ: `initData` отсутствует — это норма).
4. `https://aura-api-mptt.onrender.com/` — редирект на `/miniapp/`.

## 4.1. Сундуки: рулетка и «Сундуки обновляются»

**Анимация.** После нажатия «Открыть» сначала идёт белая вспышка ровно 1 секунда,
затем полоса предметов в стиле кейсов CS (`.roulette-container`): ячейки едут
справа налево ~6 секунд с плавным торможением, на каждой пройденной ячейке —
щелчок, по центру золотая метка. Выигрышная ячейка встаёт ровно под метку и
подсвечивается, ещё через 0.7 с открывается окно «Забрать». Награда начисляется
только по кнопке «Забрать» (`/api/claim_chest`), до этого баланс не меняется.

**Если вместо рулетки написано «Сундуки обновляются, попробуйте чуть позже»** —
сервер не смог записать награду в таблицу `chest_claims`. Причина одна из двух:

1. не применена миграция `supabase/migrations/20261030000000_capsule_stock_promo_types.sql`;
2. миграция применена, но PostgREST держит старый кэш схемы.

Проверить: `https://<сервис>/api/chest_ready` (или `!сервисы` в боте — там строка
«📦 Сундуки: …»). Ответ `{"ok": false}` содержит и точную ошибку, и файл миграции.
Лечение:

```sql
-- выполнить в Supabase → SQL Editor
-- 1) при необходимости применить файл миграции целиком
NOTIFY pgrst, 'reload schema';
```

## 5. Клиент Mini App

`miniapp/app.js` больше не привязан к захардкоженному домену: адрес API
вычисляется из `location.origin`, если страница открыта с `*.onrender.com`
(или localhost), иначе берётся `API_URL_FALLBACK`. Опечатка в домене
(`mppt`/`mptt`) больше не может сломать запросы к API.
