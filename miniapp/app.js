const tg = window.Telegram.WebApp;
tg.expand();

const user = tg.initDataUnsafe?.user;
if (user) {
    document.getElementById('user-name').innerText = user.first_name;
    document.getElementById('profile-name').innerText = user.first_name;
    // Аватар — локальная заглушка avatar_placeholder.svg: внешний via.placeholder.com
    // в WebView Telegram грузился через раз и оставлял битую картинку.
    if (user.photo_url) {
        document.getElementById('user-avatar').src = user.photo_url;
        document.getElementById('profile-avatar-large').src = user.photo_url;
    }
}

// ================= АДРЕС API =================
// Мини-аппку раздаёт сам сервис aura-api, поэтому главное правило простое:
// если страница открыта из /miniapp/ — API находится на ЭТОМ ЖЕ домене.
// Так адрес не зависит от имени сервиса в Render: опечатка в константе
// (aura-api-mppt / mptt) больше не превращает все запросы в «Not Found».
// Абсолютные адреса — только запасные, для случая «страницу открыли не с нашего
// сервера» (файл с диска, сторонний хостинг статики). Если и они не отвечают,
// приложение само находит живой сервис через /api/health (см. recoverApiUrl).
const API_URL_CANDIDATES = [
    "https://aura-api-mppt.onrender.com",
    "https://aura-api-mptt.onrender.com",
];
const API_URL_FALLBACK = API_URL_CANDIDATES[0];

function normalizeApiUrl(url) {
    return String(url === null || url === undefined ? "" : url)
        .replace(/\/+$/, "")
        // «/miniapp» в конце не нужен: дальше всегда приписывается /api/...
        .replace(/\/miniapp$/, "");
}

// Путь вида /miniapp/, /miniapp/index.html, «/» или /index.html значит
// «страницу отдал aura-api».
// Задача 38: Mini App теперь открывается и с КОРНЯ сервиса (Render health check
// перестал отвечать 405, а корень отдаёт index.html), поэтому корень тоже
// считаем «своим» — иначе app.js уходил бы на старый домен из fallback-списка
// и игрок получал «Not Found» уже на /api/....
function servedFromMiniApp(loc) {
    try {
        const path = String(loc.pathname || "/");
        if (path === "" || path === "/" || path === "/index.html") return true;
        return /^\/miniapp(\/|$)/.test(path);
    } catch (e) {
        return false;
    }
}

function resolveApiUrl(loc) {
    try {
        if (!loc || !/^https?:$/.test(loc.protocol) || !loc.hostname) return API_URL_FALLBACK;
        if (servedFromMiniApp(loc)) return normalizeApiUrl(loc.origin);
        return API_URL_FALLBACK;
    } catch (e) {
        return API_URL_FALLBACK;
    }
}

// Задача 38: Mini App мог открыться с корня сервиса, а картинки/скрипты лежат
// в /miniapp/. Берём это из тега <base>, который проставляет api.py.
const MINIAPP_BASE_DIR = (() => {
    try {
        const tag = document.querySelector('base');
        const href = tag ? String(tag.getAttribute('href') || '') : '';
        return href ? href.replace(/\/?$/, '/') : '/miniapp/';
    } catch (e) { return '/miniapp/'; }
})();
// Абсолютный адрес файла из папки Mini App (работает и с корня, и из /miniapp/).
function miniappAsset(file) {
    return `${MINIAPP_BASE_DIR}${String(file || '').replace(/^\/+/, '')}`;
}

// Адреса для проверки: сначала собственный origin (если он вообще http),
// затем текущий и известные кандидаты.
function apiUrlCandidates(current) {
    const list = [];
    try {
        if (typeof location !== "undefined" && /^https?:$/.test(location.protocol) && location.origin) {
            list.push(location.origin);
        }
    } catch (e) {}
    list.push(current);
    list.push.apply(list, API_URL_CANDIDATES);
    const seen = [];
    list.forEach(url => {
        const value = normalizeApiUrl(url);
        if (!seen.includes(value)) seen.push(value);
    });
    return seen;
}

// Живой ли это сервис: /api/health отвечает ok + service=aura-api.
// Обычный 404 «Not Found» от Render сюда не пройдёт — именно он и появлялся
// при неверном имени сервиса.
async function probeApiUrl(base, timeoutMs) {
    if (typeof fetch !== "function") return false;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller && timeoutMs
        ? setTimeout(() => { try { controller.abort(); } catch (e) {} }, timeoutMs)
        : null;
    try {
        const options = { method: "GET", cache: "no-store" };
        if (controller) options.signal = controller.signal;
        const response = await fetch(`${normalizeApiUrl(base)}/api/health`, options);
        if (!response.ok) return false;
        const data = await response.json().catch(() => null);
        return Boolean(data && data.service === "aura-api");
    } catch (e) {
        return false;
    } finally {
        if (timer) clearTimeout(timer);
    }
}

// API_URL меняется на ходу: адрес проверяется только при сбое загрузки профиля.
let API_URL = resolveApiUrl(typeof location !== "undefined" ? location : null);
let apiUrlVerified = false;

// Перебираем кандидатов и переключаемся на первый живой.
// Возвращает true, если адрес реально изменился (тогда запрос стоит повторить).
async function recoverApiUrl() {
    if (apiUrlVerified) return false;
    const candidates = apiUrlCandidates(API_URL);
    for (const base of candidates) {
        if (await probeApiUrl(base, 6000)) {
            apiUrlVerified = true;
            if (base === API_URL) return false;
            console.warn(`AURA: рабочий адрес API — ${base} (был ${API_URL || "текущий домен"})`);
            API_URL = base;
            return true;
        }
    }
    return false;
}
// Каталог приходит с сервера и уже отфильтрован: капсулы для админов не попадают в ответ обычным игрокам.
let SHOP_ITEMS = {};
let shopItemsLoaded = false;
// Актуальные картинки капсул (PNG с прозрачным фоном). Держим их и на клиенте:
// если сервер отдаст старый jpg-путь или вообще без картинки,
// магазин и инвентарь всё равно покажут нормальный арт, а не чёрный квадрат.
const SHOP_IMAGE_FALLBACK = {
    capsule: 'cap_shop_normal.png',
    uniq_capsule: 'cap_shop_uniq.png',
    ruby: 'cap_shop_ruby.png',
    diamond: 'cap_shop_diamond.png',
    galactic: 'cap_shop_galactic.png',
};
const INV_IMAGE_FALLBACK = {
    capsule: 'inv_capsule.png',
    uniq_capsule: 'inv_uniq_capsule.png',
    ruby: 'inv_ruby.png',
    diamond: 'inv_diamond.png',
    galactic: 'inv_galactic.png',
    malachite: 'inv_malachite.png',
    emerald: 'inv_emerald.png',
};

function shopImageFor(itemId, item) {
    const fromServer = item && item.shopImage ? String(item.shopImage) : '';
    // .jpg-карточки со старым чёрным фоном больше не используем.
    if (fromServer && /\.png$/i.test(fromServer)) return miniappAsset(fromServer);
    return SHOP_IMAGE_FALLBACK[itemId] ? miniappAsset(SHOP_IMAGE_FALLBACK[itemId]) : '';
}

function inventoryImageFor(itemId, item) {
    const fromServer = item && item.inventoryImage ? String(item.inventoryImage) : '';
    if (fromServer && /\.png$/i.test(fromServer)) return miniappAsset(fromServer);
    return INV_IMAGE_FALLBACK[itemId] ? miniappAsset(INV_IMAGE_FALLBACK[itemId]) : '';
}
let balance = 0, keys = 0, currentBet = 10, inventory = [], lastChestTime = 0, isProcessing = false, userDataLoaded = false;
let daysInGame = 0;
// Баланс показываем только когда он реально пришёл с сервера, иначе «…» вместо ложного 0.
let balanceKnown = false;
let profileLoadPromise = null, profileRetryTimer = null, profileRetryCount = 0, profileFailure = '';
let isAdmin = false;
// Валюта в интерфейсе — картинка монеты coin.png (эмодзи больше не используем).
const COIN_IMG_SRC = miniappAsset('coin.png');
const COIN_IMG = `<img class="coin-img" src="${COIN_IMG_SRC}" alt="монета">`;
const ADMIN_UI_IDS = [6338018270, 5184813347, 8890834384];
function isAdminId(id) { return Boolean(id) && ADMIN_UI_IDS.includes(Number(id)); }

// Ключ идемпотентности для финансовых операций. Сервер хранит ответ по этому
// ключу, поэтому двойной тап / ретрай после таймаута не спишет деньги дважды.
function requestId() {
    try {
        if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    } catch (e) {}
    return `rid-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Универсальный POST к API: JSON-ответ + понятная ошибка.
// Задача 60: таймаут в 21 секунду. Раньше запрос мог «висеть» бесконечно
// (Render просыпается, сеть в метро пропала) — и вызывающий код (проверка
// реферала, пассивка, любовь) ждал ответа всю сессию, а интерфейс оставался
// в состоянии «загружаю». Теперь по таймауту выбрасывается ошибка, и её
// обрабатывает тот же catch, что и обычный сбой.
const API_POST_TIMEOUT = 21000;

async function apiPost(path, payload, options) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => { try { controller.abort(); } catch (e) {} }, API_POST_TIMEOUT) : null;
    const init = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ initData: tg.initData }, payload || {})),
    };
    if (controller) init.signal = controller.signal;
    let res;
    try {
        res = await fetch(`${API_URL}${path}`, init);
    } catch (e) {
        const error = new Error((options && options.fallback)
            || (e && e.name === 'AbortError' ? 'Сервер не ответил вовремя' : 'Ошибка соединения'));
        error.status = 0;
        throw error;
    } finally {
        if (timer) clearTimeout(timer);
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        const error = new Error((data && data.detail) || (options && options.fallback) || 'Ошибка сервера');
        error.status = res.status;
        error.data = data;
        throw error;
    }
    return data;
}

// ================= ТЕХРАБОТЫ =================
// Играть могут только ADMIN_IDS. Остальные после приветственной заставки видят
// экран «Технические работы»: ни игры, ни магазин, ни сундуки им не открываются.
// Задача 67: игра открыта всем. Заглушка осталась только под «сервер не
// ответил» — «Технических работ» для игроков больше нет.
let maintenanceActive = false;       // показывать ли экран-заглушку
let serverMaintenance = false;       // совместимость: сервер больше не закрывает
let accessKnown = false;             // знаем ли мы, что профиль пришёл
let canPlay = false;                 // итоговое решение (после resolveAccess — true)
let maintenanceMessage = '';         // текст с сервера (если он его прислал)

// ================= НОВАЯ ЭКОНОМИКА (задача 25) =================
let passiveRates = { capsule: 0.1, uniq_capsule: 0.5, ruby: 5, diamond: 20, galactic: 50 };
// Задача 32: доход копится всегда (даже когда аппка закрыта), а ночью по МСК
// (22:00–08:00) капает вдвое меньше. Числа — с сервера (/api/user → passive_night).
let passiveNight = { multiplier: 0.5, from: 22, to: 8 };
let passiveTimer = null;
// Задача 42: сколько пассивки уже накопилось (с дробной частью). Приходит из
// /api/user → passive_pending и обновляется после «Забрать».
// Клиент НИЧЕГО не начисляет — только показывает серверное число.
let passivePending = { amount: 0, whole: 0, rate: 0, hours: 0, capped: false, night: false };
let subscriptionChecked = false;
let loveState = null;
// Задача 74: «Достижения» профиля — строки, посчитанные сервером
// (/api/profile_stats). Клиент ничего не складывает сам: только рисует.
let profileStats = null;
// Задача 75: список игроков для админ-вкладки (обновляется при её открытии).
let adminPlayers = [];
let profileStatsAt = 0;
const PROFILE_STATS_TTL = 60 * 1000;      // не чаще раза в минуту
let referralBound = false;
const PASSIVE_INTERVAL = 15 * 60 * 1000;      // раз в 15 минут, пока аппка открыта
const PASSIVE_PENDING_REFRESH = 60 * 1000;    // «накоплено» в профиле — раз в минуту
let passivePendingTimer = null;

// Задача 67: экрана «Технические работы» для игроков больше НЕТ.
// Осталась одна заглушка — «Нет связи с сервером» (Render спит / нет интернета):
// она показывается только когда профиль вообще не пришёл, и снимается сама,
// как только сервер ответил. Доступ по-прежнему решает сервер, но он теперь
// пускает всех.
function showMaintenance(options) {
    if (maintenanceActive) return;
    const maintenanceOn = Boolean(options && options.maintenance);
    maintenanceActive = true;
    serverMaintenance = maintenanceOn;
    maintenanceMessage = (options && options.message) || '';
    canPlay = false;
    // Останавливаем всё, что могло успеть стартовать, и прячем игровой интерфейс.
    try { stopHockeyPolling(); } catch (e) {}
    try { stopCrashPolling(); } catch (e) {}
    try { document.querySelectorAll('.game-screen').forEach(screen => screen.classList.remove('active')); } catch (e) {}
    document.body.classList.add('maintenance');
    const screen = document.getElementById('maintenance-screen');
    // Задача 71: два разных экрана. Техработы — это решение админа («скоро
    // вернёмся»), отсутствие связи — проблема на нашей стороне (Render спит).
    const offline = !maintenanceOn;
    if (screen) {
        screen.classList.add('active');
        screen.classList.toggle('offline', offline);
        // Заголовок и подпись зависят от причины: техработы с сервера или
        // «не дозвонились» (Render спит / нет связи). Игрок не должен читать
        // «идёт обновление», когда проблема на его стороне.
        const title = document.getElementById('maint-title');
        const sub = document.getElementById('maint-sub');
        const note = document.getElementById('maint-note');
        const icon = screen.querySelector('.maint-icon');
        if (maintenanceOn) {
            if (title) title.innerHTML = 'Технические<br>работы';
            if (sub) sub.innerHTML = 'Обновляем игру — скоро вернёмся.'
                + '<br>Коины, сундуки и Love останутся на месте.';
            if (note) note.textContent = 'Спасибо за терпение 💙';
            if (icon) icon.textContent = '🛠️';
        } else {
            if (title) title.innerHTML = 'Нет связи<br>с сервером';
            if (sub) sub.innerHTML = 'Сервер не ответил — возможно, он просыпается.'
                + '<br>Нажми «Проверить снова» через пару секунд.';
            if (note) note.textContent = 'Спасибо за терпение 💙';
            if (icon) icon.textContent = '📡';
        }
        const btn = screen.querySelector('.maint-btn');
        if (btn) {
            btn.textContent = maintenanceOn ? 'Проверить снова' : 'Проверить снова';
            btn.onclick = () => { retryAccessFromMaintenance(); };
        }
    }
    // Заставку НЕ обрываем: приветственная анимация должна доиграть (минимум 1 с),
    // а экран техработ лежит под ней и просто проявляется, когда она гаснет.
    try { hideLoader(); } catch (e) {}
}

// «Проверить снова»: перезапрос профиля без перезагрузки страницы — так
// незакрытая сессия Telegram не теряется, и игрок не видит заставку заново.
async function retryAccessFromMaintenance() {
    hideMaintenance();
    accessKnown = false;
    const ok = await loadUserData({ attempts: 2, retryDelay: 800 });
    if (ok && canPlay) { hideMaintenance(); try { updateUI(); } catch (e) {} return; }
    // Задача 67: отказ сервера по доступу больше не превращается в заглушку.
    // Не дозвонились: остаёмся на экране-заглушке, но с честным текстом.
    showMaintenance({ offline: true });
}

function hideMaintenance() {
    maintenanceActive = false;
    document.body.classList.remove('maintenance');
    const screen = document.getElementById('maintenance-screen');
    if (screen) screen.classList.remove('active');
}

// Решение «пускать или нет». Задача 67: «открой апку, чтобы все могли входить» —
// пускаем ВСЕГДА, когда профиль реально пришёл. Список админов и поле
// maintenance с сервера больше не влияют на вход: раньше достаточно было
// старого флага в окружении Render, чтобы обычный игрок упёрся в заглушку.
function resolveAccess(data) {
    // Задача 71: сервер говорит правду про техработы. Админ видит игру всегда
    // (у него же тумблер), обычный игрок — экран «Технические работы».
    const maintenanceOn = Boolean(data && (data.maintenance_active === true
        || data.maintenance === true));
    const adminHere = Boolean(isAdmin);
    serverMaintenance = maintenanceOn;
    maintenanceMessage = (data && data.maintenance_message) || '';
    accessKnown = true;
    if (maintenanceOn && !adminHere) {
        canPlay = false;
        showMaintenance({ maintenance: true, message: maintenanceMessage });
        return false;
    }
    canPlay = true;
    return true;
}

function coinAmount(value) { return `${formatCoins(value)} ${COIN_IMG}`; }
// Ник приходит от пользователя: экранируем, чтобы он не ломал вёрстку и не тащил HTML.
function escapeHtml(text) {
    return String(text === undefined || text === null ? '' : text)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
const NAME_MAX_CHARS = 16;
function shortName(name, max = NAME_MAX_CHARS) {
    const text = String(name === undefined || name === null ? '' : name).trim() || 'Игрок';
    const chars = Array.from(text);
    return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : text;
}
function nameHtml(name) { return `<span class="lb-name" title="${escapeHtml(name)}">${escapeHtml(shortName(name))}</span>`; }
// Апгрейд — только для админов (ADMIN_IDS на сервере). Флаг приходит из /api/user.
let upgradeFrom = 'coin';                                   // 'coin' | 'capsule' | 'uniq_capsule'
let upgradeFromAmount = 100;                                // коины или количество капсул
let upgradeToTab = 'coin';                                  // вкладка правой панели
const UPGRADE_DEFAULT_MULT = 1.35;                          // цель по умолчанию
let upgradeTarget = { kind: 'mult', value: UPGRADE_DEFAULT_MULT };  // mult | chance | item
let upgradeBusy = false, upgradeStartedAt = 0;
// Белая стрелка: угол в градусах (0 = вверх), rAF-цикл и «поколение» прогона,
// чтобы запоздавшие колбэки от прошлого спина не перетирали новый результат.
let upgradeArrowAngle = 0, upgradeArrowRaf = null, upgradeSpinToken = 0;
let upgradeFetchPending = false;     // ждём ответ сервера по текущей ставке
let upgradeBusyTimer = null;         // сторож «кнопка залипла»
// Задача 70: заказчик просил «5–4 секунды» вместо прежних 5.4 с + долгого
// торможения. Полный раунд — 4 с: разгон, затем плавное затухание ~2.6 с.
const UPGRADE_DURATION = 4000;        // полный раунд: быстрый спин + плавное затухание
const UPGRADE_ARROW_SPEED = 1080;     // градусов в секунду в фазе ожидания ответа сервера
const UPGRADE_ARROW_MIN_LAND = 2600;  // минимум времени на «рулеточное» затухание, мс
// Сколько ждём ответ сервера до решения «сеть/сервер не ответил». Было 8 с:
// на холодном Render (сервис просыпается 20–50 с) запрос обрывался, игрок
// видел «Сервер не ответил», хотя ставка уже была принята и сыграна.
const UPGRADE_FETCH_TIMEOUT = 45000;
const UPGRADE_MIN_COINS = 50, UPGRADE_MAX_COINS = 10000, UPGRADE_MAX_CAPSULES = 50;
const UPGRADE_MAX_CHANCE = 95;
// Задача 25: x4 и x8 убраны. x1.12…x1.75 — «почти монетка», x20/x50 — риск.
const UPGRADE_MULTS = [1.12, 1.35, 1.55, 1.75, 20, 50];
// Куда именно крутится стрелка: одна CSS-кривая на весь финиш, без рывков.
const UPGRADE_LAND_MS = 2600;
const UPGRADE_LAND_EASING = 'cubic-bezier(0.1, 0.7, 0.1, 1)';
const UPGRADE_FALLBACK_PRICES = {
    capsule: 100, uniq_capsule: 500, ruby: 1000, diamond: 5000,
    galactic: 10000, malachite: 3000, emerald: 5000,
};
// Полный каталог капсул: в апгрейд можно ставить ЛЮБУЮ капсулу из инвентаря,
// а не только обычную и уникальную. Порядок = порядок вкладок «Инвентарь».
const UPGRADE_ITEM_CATALOG = [
    { id: 'capsule', short: 'Обычная', emoji: '💊' },
    { id: 'uniq_capsule', short: 'Уник.', emoji: '⚱️' },
    { id: 'ruby', short: 'Рубиновая', emoji: '♦️' },
    { id: 'diamond', short: 'Алмазная', emoji: '💎' },
    { id: 'galactic', short: 'Галактич.', emoji: '🌌' },
    { id: 'malachite', short: 'Малахит', emoji: '🟢' },
    { id: 'emerald', short: 'Изумруд', emoji: '💚' },
];
const upgradeStats = { total: 0, wins: 0, best: 0 };
// Шанс, с которым реально играет текущий раунд. Пока он зафиксирован, круг и
// подпись рисуют ИМЕННО его: раньше любой updateUI() пересчитывал дугу под текущую
// форму, и стрелка могла стоять «в залёте», где на самом деле был проигрыш.
let upgradePlacedChance = null;
// После раунда кнопка показывает «ИГРАТЬ ЗАНОВО»: первый клик сбрасывает круг,
// второй — делает новую ставку. Без этого игрок не понимал, что делать дальше.
let upgradeAwaitingRestart = false;
// Состояние последнего результата: '' | 'spinning' | 'win' | 'lose' | 'error'.
// Нужно только для стилей текста под кнопкой: сама кнопка после раунда снова
// активна и готова к следующей ставке.
let upgradeResultState = '';
// id результатов, которые уже показывали в этой сессии: если ack не дошёл,
// один и тот же прошлый раунд не должен всплывать снова и снова.
const upgradeShownLogIds = new Set();
let hockeyInterval = null, hasShownWinner = false, shownWinnerKey = null;
// Счётчик подряд идущих неудачных опросов состояния Краша (для тихого лога).
let crashPollFailures = 0;
// Круглая арена (задача 25): вместо квадратного поля и прыгающего мяча —
// пирог из секторов и указатель, который крутится и плавно останавливается.
// Стрелка командной арены: angle — текущий угол (0..360), rafId — кадр
// непрерывного вращения, landedRound — раунд, за который стрелка уже встала
// на победителя (чтобы не «доезжать» повторно на каждом опросе).
let arenaPointer = { angle: 0, roundId: null, landedRound: null, spinning: false, rafId: null, landToken: 0,
    rollStartedAt: 0, rollTotalMs: 0 };
let arenaPollInFlight = false;
// Задача 70: ориентир старта броска для «позднего» входа. Это НЕ время
// анимации: оно нужно только чтобы понять, сколько осталось до финиша.
// Клиентское время старта (Date.now) живёт в том же масштабе, что и серверное,
// поэтому разница «сервер — клиент» считается без сюрпризов WebView.
let arenaRollClock = { roundId: null, startedAtMs: 0, totalMs: 0 };
// Задача 31: сервер держит ДВЕ арены одновременно. Здесь только то, какую из
// них смотрит игрок (view) и последний снимок обеих арен (arenas).
// mode — синоним view, оставлен для совместимости с прежним кодом и тестами.
let arenaState = {
    view: 'normal', mode: 'normal',
    arenas: {
        normal: { players: [], sectors: [], bank: 0, winner: null, winner_team: null },
        team: { players: [], sectors: [], teams: [], bank: 0, winner: null, winner_team: null },
    },
    players: [], sectors: [], teams: [], myTeam: null, status: 'waiting', last: null,
};
let hockeyServerOffsetMs = 0, hockeyClockSynced = false;

let crashInterval = null;
let lastCrashStatus = null;
let currentCrashData = null;
let crashPollPromise = null;
let crashStateUpdatedAt = 0;

let animFrameId = null;
let isAnimatingFlight = false;
let flightStartTime = 0;

// ================= ЗВУКИ =================
const AudioContextClass = window.AudioContext || window.webkitAudioContext;
const audioCtx = AudioContextClass ? new AudioContextClass() : null;
function playSound(type) {
    try {
        if (!audioCtx) return;
        if (audioCtx.state === 'suspended') audioCtx.resume();
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.connect(gain); gain.connect(audioCtx.destination);
        const now = audioCtx.currentTime;
        // Короткий шумовой всплеск: им подкрашиваем удар/взрыв, иначе слышен только тон.
        const noiseBurst = (duration, volume, cutoffStart, cutoffEnd) => {
            const length = Math.floor(audioCtx.sampleRate * duration);
            const buffer = audioCtx.createBuffer(1, length, audioCtx.sampleRate);
            const data = buffer.getChannelData(0);
            for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.exp(-i / (length * 0.25));
            const src = audioCtx.createBufferSource(); src.buffer = buffer;
            const filter = audioCtx.createBiquadFilter();
            filter.type = 'lowpass';
            filter.frequency.setValueAtTime(cutoffStart, now);
            filter.frequency.exponentialRampToValueAtTime(cutoffEnd, now + duration);
            const noiseGain = audioCtx.createGain();
            noiseGain.gain.setValueAtTime(volume, now);
            noiseGain.gain.exponentialRampToValueAtTime(0.0001, now + duration);
            src.connect(filter); filter.connect(noiseGain); noiseGain.connect(audioCtx.destination);
            src.start(now); src.stop(now + duration);
        };
        if (type === 'click') {
            osc.type = 'sine'; osc.frequency.setValueAtTime(1200, now); osc.frequency.exponentialRampToValueAtTime(800, now + 0.05);
            gain.gain.setValueAtTime(0.05, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.05);
            osc.start(now); osc.stop(now + 0.05);
        } else if (type === 'win') {
            osc.type = 'triangle'; osc.frequency.setValueAtTime(523.25, now); osc.frequency.setValueAtTime(659.25, now + 0.1); osc.frequency.setValueAtTime(783.99, now + 0.2);
            gain.gain.setValueAtTime(0.08, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
            osc.start(now); osc.stop(now + 0.4);
        } else if (type === 'lose') {
            osc.type = 'sawtooth'; osc.frequency.setValueAtTime(300, now); osc.frequency.exponentialRampToValueAtTime(100, now + 0.4);
            gain.gain.setValueAtTime(0.08, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
            osc.start(now); osc.stop(now + 0.4);
        } else if (type === 'thud') {
            // Арена: глухой «взрослый» удар по борту — низкий синус без звона.
            osc.type = 'sine';
            osc.frequency.setValueAtTime(150, now);
            osc.frequency.exponentialRampToValueAtTime(48, now + 0.2);
            gain.gain.setValueAtTime(0.0001, now);
            gain.gain.exponentialRampToValueAtTime(0.16, now + 0.012);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.24);
            osc.start(now); osc.stop(now + 0.26);
            noiseBurst(0.14, 0.06, 700, 120);
        } else if (type === 'arena_lose') {
            // Арена: проигрыш — серьёзный низкий аккорд, без «мультяшного» писка.
            osc.type = 'triangle';
            osc.frequency.setValueAtTime(196, now);
            osc.frequency.exponentialRampToValueAtTime(98, now + 0.7);
            gain.gain.setValueAtTime(0.0001, now);
            gain.gain.exponentialRampToValueAtTime(0.11, now + 0.05);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.85);
            osc.start(now); osc.stop(now + 0.9);
            const sub = audioCtx.createOscillator(); const subGain = audioCtx.createGain();
            sub.type = 'sine'; sub.frequency.setValueAtTime(98, now); sub.frequency.exponentialRampToValueAtTime(49, now + 0.7);
            subGain.gain.setValueAtTime(0.0001, now); subGain.gain.exponentialRampToValueAtTime(0.09, now + 0.08);
            subGain.gain.exponentialRampToValueAtTime(0.0001, now + 0.9);
            sub.connect(subGain); subGain.connect(audioCtx.destination);
            sub.start(now); sub.stop(now + 0.9);
        } else if (type === 'boom') {
            noiseBurst(0.8, 0.03, 400, 80);
        } else if (type === 'explosion') {
            // Ракета: объёмный взрыв — низкий гул, широкий шум и длинный хвост.
            osc.type = 'sine';
            osc.frequency.setValueAtTime(110, now);
            osc.frequency.exponentialRampToValueAtTime(28, now + 0.9);
            gain.gain.setValueAtTime(0.0001, now);
            gain.gain.exponentialRampToValueAtTime(0.2, now + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + 1.1);
            osc.start(now); osc.stop(now + 1.15);
            noiseBurst(1.2, 0.12, 2600, 90);
            noiseBurst(0.35, 0.08, 6000, 900);
        }
    } catch(e) {}
}

// ================= ОБЩИЕ =================
function updateUI() {
    const shown = balanceKnown ? balance : '…';
    document.getElementById('user-balance').innerText = shown;
    document.getElementById('profile-coins').innerText = shown;
    document.getElementById('profile-keys').innerText = keys;
    const daysEl = document.getElementById('profile-days');
    if (daysEl) daysEl.innerText = daysInGame;
    document.querySelectorAll('.game-balance-val').forEach(el => { el.innerText = shown; });
    // Задача 75: инвентарь в профиле больше не рисуем — он в «Кармане».
    renderUpgrade();
    updateUpgradeStatsUI();
    renderPocket();
}

// ================= КАРМАН (задача 72) =================
// Один экран с имуществом: баланс, ключи, Love/Unlove и весь инвентарь —
// ровно то, что бот показывает командой `карман`. Данные только с сервера.
function renderPocket() {
    const coinsEl = document.getElementById('pocket-coins');
    if (!coinsEl) return;
    coinsEl.innerHTML = balanceKnown ? coinAmount(balance) : '…';
    const keysEl = document.getElementById('pocket-keys');
    if (keysEl) keysEl.innerText = keys;
    // Любовь: у пары — ❤️ Love, одиночке — 💔 Unlove. Нет ни того, ни другого —
    // строку не показываем совсем (пустая строка «0» только путала).
    const row = document.getElementById('pocket-love-row');
    const label = document.getElementById('pocket-love-label');
    const value = document.getElementById('pocket-love');
    const inPair = Boolean(loveState && loveState.in_relationship);
    const loveAmount = inPair ? Number(loveState.love) || 0 : Number(loveState && loveState.unlove) || 0;
    if (row && label && value) {
        const show = Boolean(loveState) && (inPair || loveAmount > 0);
        row.hidden = !show;
        if (show) {
            label.textContent = inPair ? '❤️ Love' : '💔 Unlove';
            value.innerHTML = coinAmount(loveAmount);
        }
    }
    const grid = document.getElementById('pocket-inventory');
    if (grid) grid.innerHTML = inventoryGridHtml();
}

// Разметка инвентаря одна для профиля и кармана — чтобы не разъезжались.
function inventoryGridHtml() {
    const counts = {};
    inventory.forEach(itemId => { counts[itemId] = (counts[itemId] || 0) + 1; });
    const owned = Object.entries(SHOP_ITEMS).filter(([itemId]) => counts[itemId] > 0);
    if (!owned.length) return `<div class="inv-empty">📦 Инвентарь пуст</div>`;
    return owned.map(([itemId, item]) => {
        const art = inventoryImageFor(itemId, item);
        const artHtml = art
            ? `<div class="inv-art"><img src="${art}" alt="${escapeHtml(item.name)}" loading="lazy"></div>`
            : `<div class="inv-emoji">${item.emoji || '💊'}</div>`;
        return `
            <div class="inv-item">
                ${artHtml}
                <span>${escapeHtml(item.shortName || item.name)} x${counts[itemId]}</span>
            </div>
        `;
    }).join('');
}

// ================= СИНХРОНИЗАЦИЯ ПРОФИЛЯ =================
// Баланс и «карман» бота — одна и та же запись в базе. Если запрос упал
// (холодный старт Render, 500 из-за NULL в базе, нет сети), раньше аппка молча
// показывала 0. Теперь: ретраи, понятный баннер с причиной и кнопка «Повторить».
function setSyncBanner(message) {
    const banner = document.getElementById('sync-banner');
    if (!banner) return;
    if (message) {
        const text = banner.querySelector('.sync-banner-text');
        if (text) text.innerText = message;
        banner.classList.add('active');
    } else {
        banner.classList.remove('active');
    }
}

function applyProfile(data) {
    balance = Number(data.coins) || 0;
    // Задача 54: ставка уже ушла на сервер, но профиль мог прийти с балансом «до
    // списания»: показываем не больше, чем баланс до ставки минус сама ставка.
    // Раньше на экране до конца анимации висела старая сумма — это и выглядело
    // как «баланс списывается только после прокрута».
    //
    // Задача 60: обрезаем ТОЛЬКО пока блокировка Apгрейда действительно жива
    // (раунд идёт прямо сейчас). Раньше хватало флага upgradeRoundActive, а он
    // оставался взведённым после прерванного раунда — и каждый фоновый профиль
    // ставил limit = floor - stake, срезая баланс (в пределе до 0; на скрине
    // игрока так обнулилось всё при ставке 100). Просроченную блокировку снимаем.
    if (!upgradeBalanceLockLive() && (upgradeRoundActive || Number.isFinite(upgradeBalanceFloor))) {
        clearUpgradeBalanceLock();
    }
    if (upgradeBalanceLockLive()) {
        const stake = Math.max(0, Number(upgradePendingStake) || 0);
        const limit = Math.max(0, upgradeBalanceFloor - stake);
        // Пришло «до ставки» (или больше) — это устаревший ответ профиля.
        // Меньше блокировочного лимита — наоборот, свежие данные, их и берём.
        if (balance >= upgradeBalanceFloor && balance > limit) balance = limit;
    }
    keys = Number(data.keys) || 0;
    inventory = Array.isArray(data.inventory) ? data.inventory : [];
    lastChestTime = Number(data.last_chest) || 0;
    // Задача 43: «Дней в игре» — это дни с ПЕРВОГО ЗАХОДА В АППКУ.
    daysInGame = Math.max(0, Number(data.days_in_app != null ? data.days_in_app : data.days_in_game) || 0);
    if (data.passive_night && typeof data.passive_night === 'object') {
        passiveNight = Object.assign({}, passiveNight, data.passive_night);
    }
    if (data.passive_rates && typeof data.passive_rates === 'object') {
        passiveRates = Object.assign({}, passiveRates, data.passive_rates);
    }
    if (data.passive_pending && typeof data.passive_pending === 'object') {
        applyPassivePending(data.passive_pending);
    }
    // Шансы апгрейда: дом-эдж и админский множитель — с сервера, чтобы круг
    // не расходился с вердиктом.
    const upgradeConfig = data.economy && data.economy.upgrade;
    if (upgradeConfig) {
        if (Number.isFinite(Number(upgradeConfig.house_edge))) {
            upgradeEconomy.house_edge = Number(upgradeConfig.house_edge);
        }
        if (Number.isFinite(Number(upgradeConfig.chance_factor))) {
            upgradeEconomy.chance_factor = Number(upgradeConfig.chance_factor);
        }
        if (Number(upgradeConfig.chance_numerator) > 0) {
            upgradeEconomy.numerator = Number(upgradeConfig.chance_numerator);
        }
        if (upgradeConfig.multiplier_chances && typeof upgradeConfig.multiplier_chances === 'object') {
            Object.keys(upgradeConfig.multiplier_chances).forEach(key => {
                const value = Number(upgradeConfig.multiplier_chances[key]);
                if (Number.isFinite(value) && value > 0) UPGRADE_MULT_CHANCES[Number(key)] = value;
            });
        }
    }
    // Список ADMIN_IDS — главный источник; флаг сервера его подтверждает.
    isAdmin = isAdminId(user && user.id) || Boolean(data.is_admin);
    userDataLoaded = true;
    balanceKnown = true;
    profileFailure = '';
    profileRetryCount = 0;
    setSyncBanner('');
    // Задача 71: тумблер техработ в профиле админа.
    renderAdminMaintenanceCard(data);
    // Задача 67: профиль пришёл → пускаем сразу, без проверок «а админ ли».
    resolveAccess(data);
    hideMaintenance();
    updateUpgradeAccess();
    updateUI();
    // Задача 74: «Достижения» подтягиваем сразу после профиля — они у игрока
    // на виду, а сервер отвечает одним запросом.
    loadProfileStats();
    try { checkChestCooldown(); } catch (e) { console.error(e); }
    // Награда прошлого сундука, которую игрок не успел забрать (вышел на анимации).
    try { collectPendingChestReward(); } catch (e) { console.error(e); }
    renderPassiveCard();
    renderLoveBadge();
}

// Что именно не так — спрашиваем у сервера, чтобы в баннере была реальная причина.
async function diagnoseProfile() {
    try {
        const res = await fetch(`${API_URL}/api/self_check`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        if (!res.ok) return '';
        const data = await res.json().catch(() => ({}));
        if (data && data.ok && data.row) return `сервер видит ${data.row.coins} коинов`;
        return (data && data.reason) ? String(data.reason) : '';
    } catch (e) { return ''; }
}

function scheduleProfileRetry(delay = 5000) {
    if (profileRetryTimer || profileRetryCount >= 20) return;
    profileRetryTimer = setTimeout(async () => {
        profileRetryTimer = null;
        if (userDataLoaded) return;
        if (document.visibilityState === 'hidden') { scheduleProfileRetry(8000); return; }
        profileRetryCount += 1;
        const ok = await loadUserData({ attempts: 2, retryDelay: 1500 });
        // Задача 65: профиль доехал после «Нет связи с сервером» — снимаем
        // заглушку сами, игроку не нужно жать «Проверить снова».
        if (ok) {
            // Задача 67: профиль пришёл → заглушка снимается всегда.
            hideMaintenance();
            try { updateUI(); } catch (e) {}
            try { checkChestCooldown(); } catch (e) {}
            return;
        }
        scheduleProfileRetry(8000);
    }, delay);
}

// ================= ТЕХРАБОТЫ: ТУМБЛЕР АДМИНА (задача 71) =================
// Карточка есть только у админа. Игрок просто видит экран «Технические работы»
// (см. resolveAccess), а админ здесь же может открыть игру обратно — из
// приложения, без деплоя.
let maintToggleBusy = false;

// Задача 75: пункт «⚙️ Админ» в нижнем меню существует только у админа.
// У игроков его нет даже в разметке на экране (hidden), поэтому «кнопку
// техработ видят только админы» — это не уговор, а единственное состояние.
function syncAdminNav() {
    const nav = document.getElementById('nav-admin');
    const bottom = document.getElementById('main-nav');
    // Задача 75 (заказчик): «кнопку техработ видно только админам». Поэтому
    // пункт «⚙️ Админ» появляется строго у админа — и в разметке, и в меню.
    if (nav) nav.hidden = !isAdmin;
    // У админа пунктов семь — на узком экране им тесно, включаем компактный режим.
    if (bottom) bottom.classList.toggle('has-admin', isAdmin);
    const tab = document.getElementById('tab-admin');
    if (tab && !isAdmin) tab.classList.remove('active');
}

async function renderAdminPlayers() {
    const box = document.getElementById('admin-players');
    if (!box) return;
    if (adminPlayers.length) {
        box.innerHTML = adminPlayersHtml();
        return;
    }
    box.innerHTML = '<div class="ach-empty">Загрузка…</div>';
    try {
        const res = await fetch(`${API_URL}/api/global_top`);
        const payload = await res.json().catch(() => null);
        const users = Array.isArray(payload) ? payload : [];
        adminPlayers = users.slice(0, 10).map(u => ({
            name: u.username || `id${u.user_id || ''}`,
            coins: _safeCoins(u.coins),
        }));
    } catch (e) {
        adminPlayers = [];
    }
    box.innerHTML = adminPlayers.length ? adminPlayersHtml()
        : '<div class="ach-empty">Список игроков временно недоступен</div>';
}

function adminPlayersHtml() {
    return adminPlayers.map((player, index) => `
        <div class="ach-row">
            <span class="ach-icon">${index + 1}</span>
            <span class="ach-body"><span class="ach-label">${escapeHtml(player.name)}</span></span>
            <span class="ach-value">${formatCoins(player.coins)}</span>
        </div>
    `).join('');
}

function renderAdminMaintenanceCard(data) {
    const card = document.getElementById('admin-maint-card');
    syncAdminNav();
    if (!card) return;
    if (!isAdmin) { card.hidden = true; return; }
    card.hidden = false;
    const on = Boolean(data && (data.maintenance_active === true || serverMaintenance));
    const state = document.getElementById('admin-maint-state');
    const hint = document.getElementById('admin-maint-hint');
    const btn = document.getElementById('admin-maint-btn');
    if (state) {
        state.textContent = on ? 'включены' : 'выключены';
        state.classList.toggle('is-on', on);
    }
    if (hint) {
        hint.textContent = on
            ? 'Играют только админы. Игроки видят «Технические работы: скоро вернёмся».'
            : 'Выключены — играют все. Включи, чтобы закрыть игру всем, кроме админов.';
    }
    if (btn) {
        btn.textContent = on ? 'Выключить техработы' : 'Включить техработы';
        btn.classList.toggle('is-on', on);
    }
}

async function toggleMaintenance() {
    if (maintToggleBusy) return;
    maintToggleBusy = true;
    const card = document.getElementById('admin-maint-card');
    const btn = document.getElementById('admin-maint-btn');
    const enabled = Boolean(card && card.querySelector('#admin-maint-state')
        && document.getElementById('admin-maint-state').textContent !== 'включены');
    if (btn) { btn.disabled = true; btn.textContent = 'Сохраняем…'; }
    try {
        const data = await apiPost('/api/admin/set_maintenance',
            { initData: tg.initData, enabled });
        serverMaintenance = Boolean(data && data.maintenance);
        canPlay = true;
        if (!serverMaintenance) hideMaintenance();
        renderAdminMaintenanceCard({ maintenance_active: serverMaintenance });
        tg.showAlert(serverMaintenance
            ? '🛠️ Техработы включены. Игроки увидят экран «Технические работы».'
            : '✅ Техработы выключены — игра снова открыта всем.');
    } catch (e) {
        tg.showAlert(e.message || 'Не удалось переключить техработы');
        renderAdminMaintenanceCard({ maintenance_active: serverMaintenance });
    } finally {
        maintToggleBusy = false;
        if (btn) btn.disabled = false;
    }
}

async function loadUserData(options) {
    const attempts = (options && options.attempts) || 3;
    const retryDelay = (options && options.retryDelay) || 900;
    if (profileLoadPromise) return profileLoadPromise;

    profileLoadPromise = (async () => {
        let lastError = 'Не удалось загрузить профиль';
        let banned = false;

        // Одна серия запросов к текущему API_URL.
        const tryLoadProfile = async () => {
            for (let attempt = 1; attempt <= attempts; attempt++) {
                try {
                    const response = await fetch(`${API_URL}/api/user`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ initData: tg.initData })
                    });
                    const data = await response.json().catch(() => ({}));
                    if (response.status === 403) {
                        // Задача 67: сервер больше никого не закрывает по
                        // доступу, поэтому 403 означает БАН. Но если старая
                        // сборка всё же прислала «maintenance», не пугаем
                        // игрока баном — показываем «нет связи» и пробуем снова.
                        const text = String((data && data.detail) || '').toLowerCase();
                        const bannedText = text.includes('забан') || text.includes('запрет');
                        if (data && (data.maintenance === true || data.can_play === false)
                                && !bannedText) {
                            return 'maintenance';
                        }
                        banned = true;
                        tg.showAlert(data.detail || "Вы забанены");
                        try { tg.close(); } catch (e) {}
                        return 'banned';
                    }
                    if (response.ok && data && data.coins !== undefined) {
                        applyProfile(data);
                        apiUrlVerified = true;      // адрес API точно рабочий
                        return 'ok';
                    }
                    lastError = data.detail
                        ? `${data.detail} (код ${response.status})`
                        : `Сервер ответил ошибкой ${response.status}`;
                } catch (e) {
                    lastError = 'Нет связи с сервером';
                }
                if (attempt < attempts) await new Promise(r => setTimeout(r, retryDelay * attempt));
            }
            return 'failed';
        };

        let result = await tryLoadProfile();
        if (result === 'banned') return false;
        // Задача 67: «maintenance» от старой сборки — это не тупик, а повод
        // попробовать ещё раз (сервер мог не ответить). Возвращаем false.
        if (result === 'maintenance') return false;
        if (result === 'ok') return true;

        // Не получилось — возможно, в коде устаревший домен сервиса (Render на
        // несуществующем имени отвечает 404 «Not Found»). Ищем живой /api/health
        // и повторяем загрузку уже по правильному адресу.
        if (await recoverApiUrl()) {
            result = await tryLoadProfile();
            if (result === 'banned') return false;
            if (result === 'maintenance') return false;
            if (result === 'ok') return true;
        }

        profileFailure = lastError;
        balanceKnown = userDataLoaded;
        updateUI();
        const reason = await diagnoseProfile();
        const hint = reason && reason !== '' ? ` — ${reason}` : '';
        setSyncBanner(`Баланс не загрузился: ${lastError}${hint}`);
        scheduleProfileRetry();
        return false;
    })();

    try {
        return await profileLoadPromise;
    } finally {
        profileLoadPromise = null;
    }
}

// Ручная синхронизация: кнопка «Повторить» в баннере и тап по балансу.
async function refreshProfile() {
    if (profileRetryTimer) { clearTimeout(profileRetryTimer); profileRetryTimer = null; }
    profileRetryCount = 0;
    setSyncBanner('Обновляем баланс…');
    // Ручное «Повторить» — удобный момент перепроверить сам адрес сервиса.
    if (!apiUrlVerified) await recoverApiUrl();
    const ok = await loadUserData({ attempts: 2, retryDelay: 700 });
    if (!ok) setSyncBanner(profileFailure || 'Баланс не загрузился');
    return ok;
}

function showWin(text) { const popup = document.getElementById('win-popup'); if (!popup) return; popup.querySelector('.win-text').innerHTML = text; popup.classList.add('show'); setTimeout(() => popup.classList.remove('show'), 1500); }

function checkChestCooldown() {
    const now = Math.floor(Date.now() / 1000); const diff = now - lastChestTime; const btn = document.getElementById('normal-chest-btn');
    if (diff < 18000) { btn.disabled = true; btn.innerText = `КД: ${Math.floor((18000 - diff) / 3600)}ч ${Math.floor(((18000 - diff) % 3600) / 60)}м`; setTimeout(checkChestCooldown, 60000); }
    else { btn.disabled = false; btn.innerText = 'Открыть (Бесплатно)'; }
}

// ================= НАВИГАЦИЯ =================
function switchTab(tabId, element) {
    const popup = document.getElementById('win-popup'); if (popup) popup.classList.remove('show');
    document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
    if (element) element.classList.add('active');
    // Задача 75: вкладка админа. Игрок до неё не дойдёт: пункт меню скрыт, а
    // прямой вызов из консоли возвращает на «Играть».
    if (tabId === 'admin') {
        if (!isAdmin) { switchTab('play'); return; }
        renderAdminMaintenanceCard({ maintenance_active: serverMaintenance });
        adminPlayers = [];
        renderAdminPlayers();
        return;
    }
    if (tabId === 'top') { loadGlobalTop(); }
    if (tabId === 'shop') {
        // Задача 43: витрина рисуется мгновенно (кэш), обновление — фоном.
        // Раньше каждый вход на вкладку показывал «Загрузка магазина…».
        renderShop();
        loadShopItems();
    }
    if (tabId === 'pocket') {
        // Задача 72: карман рисуем из уже полученных данных и обновляем фоном.
        renderPocket();
        loadLoveState();
        if (!userDataLoaded) refreshProfile();
    }
    if (tabId === 'profile') {
        // Карточки профиля рисуем ДО сети, чтобы вкладка не «думала»:
        // пассивка уже известна, рефералы подтянутся фоном.
        renderPassiveCard();
        renderReferralStats();
        loadReferralStats(); loadLoveState();
        loadProfileStats();
        // «Сколько накопил» должно быть актуальным на момент открытия профиля:
        // пересчитываем карточку и подтягиваем серверное число (без начисления).
        renderPassiveCard();
        refreshPassivePending();
    }
}

function openGame(game) {
    // Техработы: игры не открываются, даже если экран кто-то «дожал» вручную.
    // Задача 67: игры открываются всем, canPlay здесь всегда true.
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
    document.getElementById('main-header').style.display = 'none';
    // Баннер подписки в игре не нужен: место занимает поле игры.
    const subBanner = document.getElementById('sub-banner');
    if (subBanner) subBanner.style.display = 'none';
    document.getElementById('main-nav').style.display = 'none';
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    // Экран может отсутствовать, если index.html и app.js разъехались по кэшу —
    // тогда не роняем всё приложение, а тихо игнорируем клик.
    const screen = document.getElementById('screen-' + game);
    if (!screen) { console.warn('Нет экрана для игры:', game); return; }
    screen.classList.add('active');
    if (game === 'hockey') {
        // Сразу показываем нужное поле: до первого ответа сервера режим берём
        // из последнего известного состояния, а не мигаем кругом.
        updateArenaView(arenaState.mode);
        startHockeyPolling();
    }
    if (game === 'crash') { lastCrashStatus = null; startCrashPolling(); }
    // Задача 60: вошли в Апгрейд — проверяем, что блокировка баланса от прошлого
    // раунда не осталась. Иначе первый же фоновый /api/user обрежет баланс.
    if (game === 'upgrade' && !upgradeBalanceLockLive()) clearUpgradeBalanceLock();
}

function closeGame() {
    stopHockeyPolling(); stopCrashPolling();
    // Задача 60: вышли из экрана — незавершённый раунд больше не «идёт».
    // Сторож и так снимет upgradeBusy, но блокировка баланса не должна ждать:
    // иначе баланс на главной оставался обрезанным.
    if (!upgradeFetchPending) clearUpgradeBalanceLock();
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
    document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('main-header').style.display = 'flex';
    const subBanner = document.getElementById('sub-banner');
    if (subBanner) subBanner.style.display = '';
    document.getElementById('main-nav').style.display = 'flex';
    switchTab('play', document.querySelectorAll('.nav-item')[0]);
}

function addBet(game, amount) {
    const input = document.getElementById(game + '-bet');
    if (!input) return;
    // Задача 58: пустое поле — это 0, а НЕ минимум. Раньше «+10» на пустом поле
    // молча превращало ставку в 100 (минимум арены): игрок вводил 10, нажимал
    // плюс и видел, что «оно ставит 100». Теперь кнопка честно прибавляет к тому,
    // что видно, а минимум проверяется только при отправке и подсвечивается.
    const value = parseInt(input.value);
    let current = Number.isFinite(value) ? value : 0;
    current += amount;
    const max = game === 'hockey' ? 10000 : 100000;
    if (current > max) current = max;
    if (current < 0) current = 0;
    input.value = current;
    markArenaBetTooLow(game, current);
}
function getBet(game) {
    const input = document.getElementById(game + '-bet');
    if (!input) return 0;
    const value = parseInt(input.value);
    let val = Number.isFinite(value) ? value : 0;
    const max = game === 'hockey' ? 10000 : 100000;
    if (val > max) val = max;
    if (val < 0) val = 0;
    return val;
}

// Задача 58: подсветка «ставка меньше минимума» — без молчаливой подстановки.
function markArenaBetTooLow(game, value) {
    const input = document.getElementById(game + '-bet');
    if (!input) return;
    const floor = game === 'hockey' ? ARENA_MIN_BET_CLIENT : 1;
    const tooLow = Number(value) > 0 && Number(value) < floor;
    input.classList.toggle('bet-too-low', tooLow);
    const hint = document.getElementById(game + '-bet-hint');
    if (hint) {
        hint.textContent = tooLow ? `Минимум ${floor}` : '';
        hint.classList.toggle('active', tooLow);
    }
}

// ================= ЛИДЕРБОРД =================
// Вкладка «Топ» — это только список: место | имя | баланс (💎).
// Никаких таймеров сезона, призовых мест и кнопок «?».
const CHANNEL_URL = 'https://t.me/Auragameplay_Channel';

// Кнопка подписки в шапке. Внутри Telegram — нативное открытие ссылки,
// снаружи (браузер/тест) — обычный переход по href.
//
// Задача 35: бонус выдаётся НЕ по тапу на плашку, а кнопкой в личке бота.
// Тап по плашке открывает канал и просит сервер прислать в ЛС сообщение с
// кнопкой «Проверить подписку». Бот проверяет подписку и начисляет 200 коинов,
// после чего плашка задания исчезает из аппки навсегда.
function openChannel(event) {
    try {
        if (tg && typeof tg.openTelegramLink === 'function') {
            if (event) event.preventDefault();
            tg.openTelegramLink(CHANNEL_URL);
        }
    } catch (e) { console.error('openTelegramLink недоступен', e); }
    requestChannelTask();
    return true;
}

// Плашка задания исчезает безвозвратно: бонус получен либо бот прислал задание.
function hideChannelTask() {
    const banner = document.getElementById('sub-banner');
    if (banner) { banner.hidden = true; banner.style.display = 'none'; }
    stopChannelTaskWatch();
}

// Задание уходит в личку бота вместе с кнопкой «Проверить подписку».
async function requestChannelTask() {
    if (!tg.initData) {
        if (tg && tg.showAlert) tg.showAlert('Откройте приложение через Telegram — иначе бот не сможет написать вам в личку.');
        return null;
    }
    const reward = document.getElementById('sub-banner-reward');
    try {
        const data = await apiPost('/api/channel_task', {}, { fallback: 'Не удалось отправить задание в бота' });
        if (data && data.claimed) { hideChannelTask(); return data; }
        if (reward) {
            reward.textContent = data && data.dm_sent
                ? '✅ Задание в личке бота — нажми «Проверить»'
                : `🎁 Награда за подписку: ${(data && data.bonus) || 200} коинов`;
        }
        if (data && data.dm_sent) {
            tg.showAlert('📩 Задание отправлено в личку бота.\n\nПодпишись на канал и нажми там кнопку «Проверить подписку» — бонус придёт сразу, а задание исчезнет из приложения.');
            startChannelTaskWatch();
        } else if (tg && tg.showAlert) {
            tg.showAlert((data && data.detail) || 'Бот не смог написать в личку. Открой бота и нажми «Старт».');
        }
        return data;
    } catch (e) {
        if (tg && tg.showAlert) tg.showAlert(e.message || 'Не удалось отправить задание в бота');
        return null;
    }
}

// Пока аппка открыта — раз в 10 секунд спрашиваем статус: игрок мог уже нажать
// «Проверить» в боте, и плашка задания должна пропасть сама.
let channelTaskTimer = null;
function stopChannelTaskWatch() {
    if (channelTaskTimer) { clearInterval(channelTaskTimer); channelTaskTimer = null; }
}
function startChannelTaskWatch() {
    stopChannelTaskWatch();
    let ticks = 0;
    channelTaskTimer = setInterval(() => {
        ticks += 1;
        if (ticks > 30) return stopChannelTaskWatch();   // 5 минут — хватит
        refreshChannelTask();
    }, 10000);
}

async function refreshChannelTask() {
    if (!tg.initData) return null;
    try {
        const data = await apiPost('/api/channel_status', {}, { fallback: 'Статус задания недоступен' });
        if (data && data.claimed) hideChannelTask();
        return data;
    } catch (e) { return null; }
}

// Тихая проверка при входе: если бонус уже получен — задание не показываем.
async function checkSubscription() {
    return refreshChannelTask();
}

async function loadGlobalTop() {
    const list = document.getElementById('global-top-list');
    if (!list) return;
    list.innerHTML = '<p class="lb-empty">Загрузка…</p>';
    try {
        const res = await fetch(`${API_URL}/api/global_top`);
        const payload = await res.json().catch(() => null);
        if (!res.ok) throw new Error((payload && payload.detail) || 'Топ недоступен');
        // Сервер отдаёт массив; ответ-ошибка ({detail}) не должен попадать в forEach.
        const users = Array.isArray(payload) ? payload : (Array.isArray(payload && payload.top) ? payload.top : []);
        if (users.length === 0) {
            list.innerHTML = '<p class="lb-empty">Топ пока пуст</p>';
            return;
        }
        list.innerHTML = users.map((u, i) => {
            const place = i + 1;
            const cls = place <= 3 ? `lb-item top-${place}` : 'lb-item';
            return `
                <div class="${cls}">
                    <span class="lb-place">${place}</span>
                    ${nameHtml(u.username)}
                    <span class="lb-balance">${formatCoins(_safeCoins(u.coins))} ${COIN_IMG}</span>
                </div>
            `;
        }).join('');
    } catch (e) {
        list.innerHTML = '<p class="lb-empty">Не удалось загрузить топ</p>';
    }
}

// Баланс из API может прийти числом, строкой или отсутствовать — показываем число.
function _safeCoins(value) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : 0;
}


// ================= ПАССИВНЫЙ ДОХОД =================
// Ставки живут на сервере (PASSIVE_INCOME_RATES в api.py) и приходят в профиле.
// Клиент НИЧЕГО не начисляет: он только показывает «в час» и просит сервер
// начислить накопленное (/api/passive_income).
function passiveRateFor(items) {
    const counts = {};
    (items || inventory).forEach(itemId => { counts[itemId] = (counts[itemId] || 0) + 1; });
    return Object.keys(counts).reduce((sum, itemId) => {
        const rate = Number(passiveRates[itemId] || 0);
        return sum + rate * counts[itemId];
    }, 0);
}

// Серверное «сколько накопил» → состояние карточки.
function applyPassivePending(payload) {
    if (!payload || typeof payload !== 'object') return;
    passivePending = {
        amount: Number(payload.amount || 0),
        whole: Number(payload.whole || 0),
        rate: Number(payload.rate || 0),
        hours: Number(payload.hours || 0),
        capped: Boolean(payload.capped),
        night: Boolean(payload.night),
    };
    renderPassiveCard();
}

// Формат накопленного: до 10 коинов показываем с десятыми («3.4»), дальше —
// целыми: игроку важна сумма, а не «130.123».
function formatPassiveAmount(amount) {
    const value = Math.max(0, Number(amount) || 0);
    if (value <= 0) return '0';
    if (value < 10) return value.toFixed(1).replace(/\.0$/, '');
    return String(Math.floor(value));
}

// Задача 43: «i» на карточке пассивки — на месте «Накоплено» показываем
// объяснение (как капает, ночная ставка, почему только целые коины).
function togglePassiveInfo(force) {
    const info = document.getElementById('passive-info');
    const body = document.getElementById('passive-body');
    const btn = document.getElementById('passive-info-btn');
    if (!info) return;
    const show = force === undefined ? info.hidden : Boolean(force);
    info.hidden = !show;
    if (body) body.style.display = show ? 'none' : '';
    if (btn) {
        btn.classList.toggle('active', show);
        btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    }
}

// То же для рефералов: «i» открывает «за кого какая награда».
function toggleReferralInfo(force) {
    const info = document.getElementById('ref-info');
    const btn = document.getElementById('ref-info-btn');
    if (!info) return;
    const show = force === undefined ? info.hidden : Boolean(force);
    info.hidden = !show;
    if (btn) {
        btn.classList.toggle('active', show);
        btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    }
}

function renderPassivePending() {
    const el = document.getElementById('passive-pending');
    if (!el) return;
    const amount = Number(passivePending.amount || 0);
    el.innerHTML = `${formatPassiveAmount(amount)} ${COIN_IMG}`;
    el.classList.toggle('zero', amount < 1);
    el.title = passivePending.capped
        ? 'Накоплено максимум за 24 часа — забери, дальше счёт пойдёт заново'
        : 'Столько уже накапало — жми «Забрать»';
}

function renderPassiveCard() {
    const rateEl = document.getElementById('passive-rate');
    const hintEl = document.getElementById('passive-hint');
    const button = document.getElementById('passive-btn');
    if (!rateEl) return;
    // Задача 56: ставку присылает сервер (и она учитывает ровно тот инвентарь,
    // по которому начисляет). Локальный расчёт оставляем запасным: раньше
    // карточка считала по своему списку и могла показать «0.0 / час», хотя
    // сервер капал коины.
    const localRate = passiveRateFor();
    const rate = Number(passivePending.rate) > 0 ? Number(passivePending.rate) : localRate;
    // Монета — наша картинка AG (coin.png), эмодзи больше не используем.
    rateEl.innerHTML = `${rate.toFixed(1)} ${COIN_IMG} / час`;
    renderPassivePending();
    if (hintEl) {
        const nightFrom = String(passiveNight.from || 22).padStart(2, '0');
        const nightTo = String(passiveNight.to || 8).padStart(2, '0');
        const nightNote = passivePending.night ? ' Сейчас ночь — капает вдвое меньше.' : '';
        hintEl.textContent = rate > 0
            ? `Твой инвентарь приносит ${rate.toFixed(1)} коинов каждый час. Доход копится, даже когда аппка закрыта `
              + `(ночью ${nightFrom}:00–${nightTo}:00 МСК — вдвое меньше).${nightNote}`
            : 'Купи капсулы — они приносят коины каждый час, даже когда аппка закрыта.';
        // «Что мешает забрать» — прямо в подсказке, чтобы кнопка не выглядела сломанной.
        const whole = Number(passivePending.whole || 0);
        if (rate > 0 && whole < 1) {
            const need = Math.max(0, 1 - Number(passivePending.amount || 0));
            hintEl.textContent = `Копится: ${formatPassiveAmount(passivePending.amount)} коинов. `
                + `До первой выплаты ${formatPassiveAmount(need)} — целый коин забирается целиком.`;
        }
    }
    if (button) {
        // Забирать можно только когда накопился хотя бы 1 целый коин:
        // сервер отдаёт целые, а дробную часть держит до следующего раза.
        button.disabled = rate <= 0 || Number(passivePending.whole || 0) < 1;
        button.textContent = Number(passivePending.whole || 0) >= 1
            ? `Забрать ${Math.floor(Number(passivePending.whole) || 0)}`
            : 'Забрать';
    }
}

async function claimPassiveIncome(options) {
    const silent = Boolean(options && options.silent);
    if (!tg.initData) return null;
    const button = document.getElementById('passive-btn');
    if (button && !silent) { button.disabled = true; button.textContent = 'Начисляем…'; }
    try {
        const data = await apiPost('/api/passive_income', {}, { fallback: 'Пассивный доход недоступен' });
        if (data.accrued > 0) {
            playSound('coin');
            if (!silent) {
                const nightNote = data.night ? '\n🌙 Сейчас ночь — капает вдвое меньше (22:00–08:00 МСК).' : '';
                tg.showAlert(`💤 Пассивный доход: +${data.accrued} коинов${nightNote}`);
            }
            balance = Number(data.new_balance) || balance;
            balanceKnown = true;
            applyPassivePending({
                amount: Number(data.pending || 0),
                whole: Math.floor(Number(data.pending || 0)),
                rate: Number(data.rate || 0),
                hours: Number(data.hours || 0),
                night: Boolean(data.night),
                capped: Boolean(data.capped),
            });
            updateUI();
        } else if (!silent) {
            const pending = Number(data.pending || data.entitled || 0);
            // Задача 56: объясняем ПРИЧИНУ понятным языком. Раньше игрок видел
            // «Накопление идёт» / «Нет капсул — копи коины» и не понимал, почему
            // кнопка ничего не выдала.
            let hint;
            if (Number(data.rate || 0) <= 0) {
                hint = 'Купи капсулы в магазине — они приносят коины каждый час, '
                    + 'даже когда приложение закрыто.';
            } else {
                const need = Math.max(0, 1 - pending);
                hint = `💤 Копится: ${formatPassiveAmount(pending)} коинов. `
                    + `До выплаты ещё ${formatPassiveAmount(need)} — забираются только целые коины, `
                    + 'дробная часть не сгорает.';
            }
            tg.showAlert(hint);
        }
        if (data && data.pending !== undefined) {
            applyPassivePending({
                amount: Number(data.pending || 0),
                whole: Math.floor(Number(data.pending || 0)),
                rate: Number(data.rate || 0),
                hours: Number(data.hours || 0),
                night: Boolean(data.night),
            });
        }
        return data;
    } catch (e) {
        if (!silent) tg.showAlert(e.message || 'Не удалось начислить пассивный доход');
        return null;
    } finally {
        if (button) { button.textContent = 'Забрать'; renderPassiveCard(); }
    }
}

// Задача 42: запросить у сервера, сколько уже накоплено (ничего не начисляет).
async function refreshPassivePending() {
    if (!tg.initData || !canPlay) return null;
    try {
        const data = await apiPost('/api/passive_preview', {}, { silent: true });
        if (data && data.ok) applyPassivePending(data);
        return data;
    } catch (e) {
        return null;
    }
}

function startPassiveIncomeTimer() {
    if (passiveTimer) return;
    // Задача 45: раз в PASSIVE_INTERVAL только ПЕРЕСЧИТЫВАЕМ «сколько накоплено».
    // Раньше здесь вызывался claimPassiveIncome — коины падали на баланс сами,
    // без нажатия «Забрать».
    passiveTimer = setInterval(() => {
        if (document.visibilityState === 'visible' && canPlay) refreshPassivePending();
    }, PASSIVE_INTERVAL);
    // Пока профиль открыт, обновляем «накоплено» раз в минуту: игрок видит,
    // как сумма растёт, не перезаходя в приложение.
    if (!passivePendingTimer) {
        passivePendingTimer = setInterval(() => {
            const profile = document.getElementById('tab-profile');
            if (document.visibilityState === 'visible' && profile && profile.classList.contains('active')) {
                refreshPassivePending();
            }
        }, PASSIVE_PENDING_REFRESH);
    }
}

// ================= РЕФЕРАЛЫ =================
// Код приглашения — ref_<id>; ссылка вида
// https://t.me/<бот>?startapp=ref_<id>. Награда начисляется пригласившему,
// когда приглашённый заходит в Mini App (сервер проверяет его NFT/premium).
function referralCodeFor(userId) { return `ref_${Number(userId) || 0}`; }

// Задача 60: код приглашения ищем по-разному, потому что ссылка бывает двух
// видов: `?startapp=ref_<id>` (start_param) и `?start=ref_<id>` (startapp-параметр
// приходит как start_param, а обычный start — как start_param тоже, но старые
// ссылки несли `start=ref_...`). Раньше любая строка, где встречалось «ref»,
// уходила на сервер как код — и привязка молча не срабатывала.
function referralCodeFromParams() {
    const fromStart = (tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
    const candidates = [fromStart];
    try {
        const query = new URLSearchParams((window.location && window.location.search) || '');
        candidates.push(query.get('startapp') || '', query.get('start') || '', query.get('ref') || '');
    } catch (e) {}
    // Иногда Telegram присылает строку целиком: «startapp=ref_123».
    for (const raw of candidates) {
        let text = String(raw || '').trim();
        if (!text) continue;
        if (text.includes('startapp=')) text = text.split('startapp=')[1];
        else if (text.includes('start=')) text = text.split('start=')[1];
        text = text.split('&')[0].toLowerCase();
        if (text.startsWith('ref_')) text = text.slice(4);
        // id в Telegram — числа; 1–2 знака тоже принимаем (это стенд/тест),
        // а настоящую проверку всё равно делает сервер (/api/referral_bind).
        if (/^\d{1,12}$/.test(text)) return `ref_${text}`;
    }
    return '';
}

// Привязка живёт ровно один раз за запуск, но если сервер ответил ошибкой
// (сеть/таблица), попытку нужно повторить при следующем входе — раньше флаг
// ставился ДО запроса, и одна неудача «съедала» приглашение навсегда.
async function bindReferralFromStartParam() {
    if (referralBound) return;
    const code = referralCodeFromParams();
    if (!code) return;
    try {
        const data = await apiPost('/api/referral_bind', { ref_code: code }, { silent: true });
        // «Уже учтено», «нельзя себя» и «пригласивший не найден» — окончательные
        // ответы, повторять их незачем. Ошибкой считаем только отсутствие ответа.
        if (data && (data.ok || data.already || data.detail)) referralBound = true;
        if (data && data.detail) console.info('Реферал:', data.detail);
    } catch (e) { console.warn('Не удалось привязать реферала', e); }
}

async function checkReferralStatus() {
    if (!tg.initData) return null;
    try {
        const data = await apiPost('/api/referral_check', {}, { silent: true });
        if (data && data.paid && data.reward) {
            console.info('Реферальная награда начислена:', data.reward);
        }
        // Награда могла прийти пригласившему прямо сейчас — обновляем карточку,
        // иначе «Заработано» показывало старую цифру до перезахода.
        if (data && data.referred) loadReferralStats();
        return data;
    } catch (e) { console.warn('referral_check', e); return null; }
}

// Задача 43: последние цифры рефералов показываем мгновенно из памяти/localStorage,
// затем обновляем фоном — карточка больше не «ждёт» сервер.
const REF_CACHE_KEY = 'aura_ref_cache_v1';
let referralStats = (() => {
    try { return JSON.parse(localStorage.getItem(REF_CACHE_KEY) || 'null') || null; }
    catch (e) { return null; }
})();

// Задача 43: текст «i» собираем из чисел сервера (reward_rules), а не из
// захардкоженного диапазона. Если правил нет — остаётся статичный текст из HTML.
// Правила рефералов — ДОСЛОВНО формулировка заказчика (задача 44).
// Слова «капсулы» здесь быть не должно: речь о НФТ и подарках в профиле.
function renderReferralRules(rules) {
    const box = document.getElementById('ref-info');
    if (!box) return;
    const base = Number((rules && rules.base) || 500);
    const nft2 = Number((rules && rules.nft2) || 800);
    const premium = Number((rules && rules.premium) || 2000);
    box.innerHTML = '<b>За каждого реферала</b><br>'
        + `Обычного даётся = <b>${formatCoins(base)}</b> коинов<br>`
        + `Обычный с минимум 2 нфт и подарками в профиле = <b>${formatCoins(nft2)}</b> коинов<br>`
        + `За премиум с рейтингом и минимум 1 нфт = <b>${formatCoins(premium)}</b> коинов`;
}

function renderReferralStats() {
    // Правила видны всегда: цифры приходят из API, но если ответа нет —
    // показываем базовые 500 / 800 / 2000, чтобы «подробнее» не было пустым.
    if (document.getElementById('ref-info')) {
        renderReferralRules((referralStats && referralStats.reward_rules) || null);
    }
    if (!referralStats) return;
    const countEl = document.getElementById('ref-count');
    const earnedEl = document.getElementById('ref-earned');
    const linkEl = document.getElementById('ref-link');
    if (countEl) countEl.textContent = String(referralStats.count || 0);
    if (earnedEl) earnedEl.textContent = String(formatCoins(referralStats.earned || 0));
    if (linkEl && !linkEl.value && referralStats.link) linkEl.value = referralStats.link;
}

async function loadReferralStats() {
    if (!tg.initData) return;
    try {
        const data = await apiPost('/api/referrals', {});
        const countEl = document.getElementById('ref-count');
        const earnedEl = document.getElementById('ref-earned');
        const linkEl = document.getElementById('ref-link');
        const detailEl = document.getElementById('ref-detail');
        // В шапке карточки «Заработано» — только число: монета уже нарисована
        // рядом в разметке (задача 43: раньше монета дублировалась).
        if (countEl) countEl.textContent = String(data.count || 0);
        if (earnedEl) earnedEl.textContent = String(formatCoins(data.earned || 0));
        // Имя бота берём у СЕРВЕРА. Раньше здесь был initDataUnsafe.bot_username,
        // то есть имя того бота, из которого открыли страницу: у проекта есть
        // старый бот, и игроки получали ссылку-приглашение на него.
        const linkBot = (data.link && /t\.me\/([^/?]+)/i.exec(data.link) || [])[1] || '';
        const botName = data.bot_username
            || (linkBot && linkBot !== '{bot}' ? linkBot : '')
            || 'AuragameBot';
        const link = data.link
            ? data.link.replace('{bot}', botName)
            : `https://t.me/${botName}?startapp=${referralCodeFor(user && user.id)}`;
        if (linkEl) linkEl.value = link;
        if (detailEl) detailEl.textContent = data.available === false ? (data.detail || '') : '';
        referralStats = {count: Number(data.count) || 0,
                         earned: Number(data.earned_paid != null ? data.earned_paid : data.earned) || 0,
                         link: link};
        renderReferralRules(data.reward_rules);
        try { localStorage.setItem(REF_CACHE_KEY, JSON.stringify(referralStats)); } catch (e) {}
    } catch (e) { console.warn('referrals', e); }
}

function copyReferralLink() {
    const linkEl = document.getElementById('ref-link');
    const link = linkEl ? linkEl.value : '';
    if (!link) {
        tg.showAlert('Ссылка появится через пару секунд — открой профиль ещё раз.');
        return;
    }
    const copy = (text) => {
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
        } catch (e) {}
        return Promise.reject(e);
    };
    // Задача 44: кнопка «Позвать друга» должна ещё и поделиться ссылкой —
    // так приглашение в один тап, а не «скопируй и вставь сам».
    const done = () => {
        try {
            if (tg && typeof tg.shareUrl === 'function') { tg.shareUrl(link); return; }
        } catch (e) {}
        tg.showAlert('Ссылка скопирована — отправь её другу 🎁');
    };
    copy(link).then(done).catch(() => tg.showAlert(`Скопируй ссылку вручную: ${link}`));
}

// ================= ОТНОШЕНИЯ =================
async function loadLoveState() {
    if (!tg.initData) return;
    try {
        loveState = await apiPost('/api/love_state', {});
        renderLoveBadge();
        renderPocket();
        if (loveState && todayStageRewards(loveState)) {
            // Этапы и награды — секретная механика: не раскрываем ни название,
            // ни сумму, только мягко сообщаем, что стало теплее.
            tg.showAlert('💗 Ваши отношения стали ещё крепче!');
            await loadUserData().catch(() => {});
        }
    } catch (e) { console.warn('love_state', e); }
}

// ================= ДОСТИЖЕНИЯ ПРОФИЛЯ (задача 74) =================
// «Профиль — это профиль»: тут факты про игрока (сколько потратил, лучший
// занос, лучший апгрейд), а имущество остаётся в «Кармане».
async function loadProfileStats(force) {
    if (!tg.initData) return;
    if (!force && profileStats && (Date.now() - profileStatsAt) < PROFILE_STATS_TTL) {
        renderAchievements();
        return;
    }
    try {
        const data = await apiPost('/api/profile_stats', {});
        if (data && data.ok) {
            profileStats = data;
            profileStatsAt = Date.now();
        }
    } catch (e) {
        // Профиль не должен ломаться из-за статистики: показываем что есть.
        console.warn('profile_stats', e);
    }
    renderAchievements();
}

// Только осмысленные строки: пустое значение — «—», а не «0 коинов».
function achievementRows() {
    const rows = (profileStats && Array.isArray(profileStats.rows)) ? profileStats.rows : [];
    return rows.map(row => {
        const valueText = row.value_text != null ? String(row.value_text) : '';
        const text = row.text ? String(row.text) : '';
        // Ни числа, ни текста — момент ещё не случился.
        const empty = (!valueText || valueText === '—') && !text;
        return { row, valueText: empty ? '—' : (text || valueText), empty };
    });
}

function renderAchievements() {
    const list = document.getElementById('achievements-list');
    if (!list) return;
    const rows = achievementRows();
    const daysEl = document.getElementById('ach-days');
    const days = profileStats ? Math.max(0, Number(profileStats.days) || 0) : daysInGame;
    if (daysEl) daysEl.innerText = `${days} дн.`;
    if (!rows.length) {
        list.innerHTML = '<div class="ach-empty">Считаем…</div>';
        return;
    }
    list.innerHTML = rows.map(({ row, valueText, empty }) => `
        <div class="ach-row${empty ? ' is-empty' : ''}">
            <span class="ach-icon">${row.icon || '•'}</span>
            <span class="ach-body">
                <span class="ach-label">${escapeHtml(row.label || '')}</span>
                ${row.hint ? `<span class="ach-hint">${escapeHtml(row.hint)}</span>` : ''}
            </span>
            <span class="ach-value">${escapeHtml(valueText)}</span>
        </div>
    `).join('');
    const hint = document.getElementById('ach-hint');
    if (hint) {
        hint.innerText = (profileStats && profileStats.ready === false)
            ? 'Статистика начнёт копиться с этого захода — обнови приложение позже.'
            : 'Цифры копятся с первого дня в приложении и не сгорают.';
    }
}

function todayStageRewards(state) {
    if (!state || !state.stage_up) return false;
    if (state._shownRewards) return false;
    state._shownRewards = true;
    return true;
}

// «Свободен» / «Свободна»: Telegram не отдаёт пол, поэтому смотрим на имя.
// Не угадали — показываем нейтральное «Свободен(на)», это лучше пустого места.
function freeLoveLabel() {
    const name = String((user && (user.first_name || user.username)) || '').trim();
    if (!name) return 'Свободен(на)';
    return /[ая]$/i.test(name) ? 'Свободна' : 'Свободен';
}

function renderLoveBadge() {
    const badge = document.getElementById('profile-love-badge');
    const freeBadge = document.getElementById('profile-love-free');
    const stage = document.getElementById('profile-love-stage');
    const freeText = document.getElementById('profile-love-free-text');
    const freeStageEl = document.getElementById('profile-love-free-stage');
    if (!badge && !freeBadge) return;
    const inRelationship = Boolean(loveState && loveState.in_relationship);
    if (badge) badge.hidden = !inRelationship;
    if (freeBadge) freeBadge.hidden = inRelationship;
    if (!inRelationship) {
        if (freeText) freeText.textContent = freeLoveLabel();
        // Задача 63: в плашке оставляем ТОЛЬКО уровень — деньги (Unlove) из неё
        // убраны по просьбе заказчика: сумма в плашке дублировала баланс и
        // путала («ур. 1 · 10 Unlove» рядом с коинами). Счёт игрока виден в
        // карточке отношений и в «любовь инфа».
        if (freeStageEl) {
            const level = Number(loveState && loveState.solo_level) || 1;
            freeStageEl.textContent = ` · ур. ${level}`;
        }
        return;
    }
    // Задача 60: в плашке остаётся ТОЛЬКО название ранга. Раньше там же
    // висели партнёр (`id1615872008` — сырой id из базы), «0 Love», «до ур. 2: 50»
    // и дни: игрок видел четыре разных числа и не понимал, что из этого важно.
    // Уровень и прогресс остаются в карточке отношений, в плашке — только ранг.
    if (stage) {
        const title = loveState.stage && loveState.stage.title ? String(loveState.stage.title) : '';
        stage.textContent = title;
    }
}

// ================= ПОПОЛНЕНИЕ / TELEGRAM STARS =================
// Кнопка «+» у баланса и карточка «Telegram Stars» открывают один и тот же
// экран с пакетами. Список цен приходит с сервера (/api/stars_packages), счёт
// создаёт /api/stars_invoice, а начисление делает бот по successful_payment —
// клиент деньги не рисует.
let topUpPackages = null;
let topUpBusy = false;
// Покупка коинов за Telegram Stars пока выключена: в профиле карточка размыта
// с надписью «Скоро…», а кнопка «+» у баланса честно говорит то же самое.
// Когда включим — достаточно поставить true и вернуть кнопке обработчик.
// Задача 70: донатная система включена — прайс и бонусы приходят с сервера.
let STARS_SOON = false;
const STARS_SOON_TEXT = 'Скоро… Пополнение за Telegram Stars ещё готовится.';

function topUpModal() { return document.getElementById('topup-modal'); }

function closeTopUp() {
    const modal = topUpModal();
    if (modal) modal.classList.remove('active');
}

function renderTopUpList(message) {
    const list = document.getElementById('topup-list');
    if (!list) return;
    const packages = Array.isArray(topUpPackages) ? topUpPackages : [];
    if (!packages.length) {
        list.innerHTML = `<div class="topup-loading">${message || 'Пакеты недоступны'}</div>`;
        return;
    }
    // Задача 70: пакет с бонусом подсвечивается радужным и показывает
    // «+N бонусом» — игрок видит, что за крупный донат дают больше коинов.
    list.innerHTML = packages.map(pkg => {
        const bonus = Number(pkg.bonus) || 0;
        const total = Number(pkg.total) || (Number(pkg.coins) || 0) + bonus;
        const bonusHtml = bonus > 0
            ? `<span class="topup-bonus">+${formatCoins(bonus)} бонус</span>` : '';
        return `
        <button class="topup-item${bonus > 0 ? ' is-bonus' : ''}" data-package="${escapeHtml(pkg.id)}">
            <span class="topup-item-coins">
                <img src="coin.png" alt="монета"> ${formatCoins(total)}
            </span>
            ${bonusHtml}
            <span class="topup-item-price">⭐ ${Number(pkg.stars) || 0}</span>
        </button>`;
    }).join('');
    list.querySelectorAll('.topup-item').forEach(btn => {
        btn.addEventListener('click', () => buyCoinPackage(btn.dataset.package, btn));
    });
}

function applyStarsSoonState() {
    const card = document.getElementById('stars-card');
    if (!card) return;
    card.classList.toggle('is-soon', STARS_SOON);
    // Задача 73: плашки «Скоро…» больше нет — она висела поверх профиля.
    // Если старая разметка всё же приедет из кэша, гасим её принудительно.
    document.querySelectorAll('.soon-overlay').forEach(el => {
        el.hidden = true;
        el.style.display = 'none';
    });
    const button = document.getElementById('stars-btn');
    if (button) {
        button.textContent = STARS_SOON ? 'Скоро…' : 'Открыть магазин Stars';
        button.onclick = STARS_SOON ? showStarsSoon : openTopUp;
    }
    // Кнопка «+» у баланса: пока «скоро» — не открывает пустую модалку.
    const plus = document.querySelector('.plus-btn');
    if (plus) {
        plus.title = STARS_SOON ? 'Скоро…' : 'Пополнить баланс';
        plus.classList.toggle('soon', STARS_SOON);
    }
}

function showStarsSoon() {
    if (tg && tg.showAlert) tg.showAlert(STARS_SOON_TEXT);
}

async function openTopUp() {
    if (STARS_SOON) { showStarsSoon(); return; }
    const modal = topUpModal();
    if (!modal) {
        // index.html и app.js разъехались по кэшу — не молчим, а говорим причину.
        if (tg && tg.showAlert) tg.showAlert('Обновите приложение: экран пополнения ещё не загрузился.');
        return;
    }
    modal.classList.add('active');
    const list = document.getElementById('topup-list');
    if (list) list.innerHTML = '<div class="topup-loading">Загрузка…</div>';
    try {
        const res = await fetch(`${API_URL}/api/stars_packages`);
        const data = await res.json().catch(() => ({}));
        topUpPackages = Array.isArray(data.packages) ? data.packages : [];
        if (data.enabled === false && topUpPackages.length) {
            renderTopUpList('Пополнение временно выключено');
            return;
        }
        renderTopUpList('Пакеты недоступны');
    } catch (e) {
        renderTopUpList('Не удалось загрузить пакеты. Проверьте соединение.');
    }
}

async function buyCoinPackage(packageId, button) {
    if (!packageId || topUpBusy) return;
    topUpBusy = true;
    const label = button ? button.innerHTML : '';
    if (button) { button.disabled = true; button.innerHTML = '<span class="topup-item-coins">Создаём счёт…</span>'; }
    try {
        const data = await apiPost('/api/stars_invoice', { package_id: packageId });
        if (!data || data.enabled === false || !data.url) {
            const list = (data && data.packages ? data.packages : topUpPackages) || [];
            const text = list.map(p => `⭐ ${p.stars} → ${formatCoins(p.coins)} коинов`).join('\n');
            tg.showAlert(`${(data && data.detail) || 'Пополнение пока недоступно'}\n\n${text}`);
            return;
        }
        closeTopUp();
        if (tg.openInvoice) {
            tg.openInvoice(data.url, (status) => {
                if (status === 'paid') {
                    tg.showAlert('⭐ Оплата прошла! Коины придут в бот сообщением.');
                    loadUserData().catch(() => {});
                }
            });
        } else if (tg.openTelegramLink) {
            tg.openTelegramLink(data.url);
        } else {
            window.open(data.url, '_blank');
        }
    } catch (e) {
        tg.showAlert(e.message || 'Не удалось создать счёт');
    } finally {
        topUpBusy = false;
        if (button) { button.disabled = false; button.innerHTML = label; }
    }
}

// Совместимость: старые места звали openStarsShop().
function openStarsShop() { return openTopUp(); }

// ================= СУНДУКИ =================
// Сервер только РАЗЫГРЫВАЕТ награду (/api/open_chest), а начисляет её
// /api/claim_chest после кнопки «Забрать». До этого баланс не меняется —
// раньше коины приходили ещё до анимации рулетки, это был баг.
let pendingChestClaimId = null, chestFlashTimer = null;
// Капсула, которую показывает открытый сундук (сервер начислит её по «Забрать»).
let chestPendingItem = null;
// true — награда этого сундука начислена сразу при открытии (старая схема,
// когда chest_claims недоступна): кнопка «Забрать» только закрывает окно.
let chestRewardCredited = false;

// Капсула из сундука (задача 22): шансы те же, что на сервере — иначе рулетка
// врала бы о вероятностях. Золотой сундук 10 %, обычный 0.1 %.
const CHEST_CAPSULE_ITEM = 'capsule';
const CHEST_CAPSULE_IMG = 'inv_capsule.png';
const CHEST_CAPSULE_CHANCE_NORMAL = 0.001;
const CHEST_CAPSULE_CHANCE_GOLD = 0.10;

function chestCapsuleChance(isGold) {
    return isGold ? CHEST_CAPSULE_CHANCE_GOLD : CHEST_CAPSULE_CHANCE_NORMAL;
}

// Ячейка рулетки: ключ / монеты / капсула.
function chestRouletteItemHtml(kind, amount) {
    if (kind === 'key') return '🔑<span>Ключ</span>';
    if (kind === 'capsule') {
        return `<img class="cap-img" src="${CHEST_CAPSULE_IMG}" alt="капсула" loading="lazy">` +
               '<span>Капсула</span>';
    }
    return `${COIN_IMG}<span>${amount}</span>`;
}

// Подпись капсулы для окна награды: картинка маленькая, поэтому и в тексте.
function chestCapsuleText(amount) {
    const count = Math.max(1, Number(amount) || 1);
    return `<img class="cap-img" src="${CHEST_CAPSULE_IMG}" alt="капсула"> Обычная капсула × ${count}`;
}

// Белая вспышка ровно на секунду, затем крутим рулетку.
function playChestFlash(durationMs = 1000) {
    const flash = document.getElementById('chest-flash');
    if (!flash) return Promise.resolve();
    // Перезапуск анимации, если вспышку вызвали повторно.
    flash.classList.remove('active');
    void flash.offsetWidth;
    flash.classList.add('active');
    try { tg.HapticFeedback && tg.HapticFeedback.impactOccurred('medium'); } catch (e) {}
    if (chestFlashTimer) clearTimeout(chestFlashTimer);
    return new Promise(resolve => {
        chestFlashTimer = setTimeout(() => {
            flash.classList.remove('active');
            chestFlashTimer = null;
            resolve();
        }, durationMs);
    });
}

// Задача 70: награда сундука не должна теряться, если игрок вышел из аппки на
// анимации. Сервер держит её в chest_claims незабранной; при заходе забираем
// автоматически и говорим игроку, что пришло.
let chestPendingCheckedAt = 0;
async function collectPendingChestReward(force) {
    if (!tg.initData) return false;
    // Не чаще раза в минуту, кроме явного вызова после открытия сундука.
    if (!force && Date.now() - chestPendingCheckedAt < 60000) return false;
    chestPendingCheckedAt = Date.now();
    try {
        const data = await apiPost('/api/chest_pending_claim', {}, { fallback: '' });
        if (!data || !data.claimed) return false;
        const chestBalance = Number(data.new_balance);
        if (Number.isFinite(chestBalance)) { balance = chestBalance; balanceKnown = true; }
        if (Number.isFinite(Number(data.new_keys))) keys = Number(data.new_keys);
        if (Array.isArray(data.items) && data.items.length) {
            inventory = inventory.concat(data.items);
        }
        updateUI();
        const parts = [];
        if (Number(data.reward_keys) > 0) parts.push('🔑 Золотой ключ');
        if (Number(data.reward_coins) > 0) parts.push(coinAmount(Number(data.reward_coins)));
        if (Array.isArray(data.items) && data.items.length) parts.push(chestCapsuleText(data.items.length));
        if (parts.length) {
            tg.showAlert(`🎁 Награда за сундук зачислена: ${parts.join(' и ')}`);
            playSound('win');
        }
        return true;
    } catch (e) {
        return false;
    }
}

function hideChestRoulette() {
    const roulette = document.getElementById('roulette-container');
    if (roulette) {
        roulette.style.display = 'none';
        roulette.classList.remove('spinning', 'hit');
    }
    const track = document.getElementById('roulette-track');
    if (track) { track.style.transform = 'translateX(0)'; track.innerHTML = ''; }
    const caption = document.getElementById('roulette-caption');
    if (caption) caption.textContent = 'Открываем сундук…';
    const buttons = document.getElementById('chest-buttons');
    if (buttons) buttons.style.display = 'flex';
    const modal = document.getElementById('chest-win-modal');
    if (modal) modal.classList.remove('active');
    pendingChestClaimId = null;
    chestRewardCredited = false;
    chestPendingItem = null;
    checkChestCooldown();
}

// ================= РУЛЕТКА СУНДУКА (как в кейсах CS) =================
// Полоса предметов едет справа налево и плавно тормозит; на каждой пройденной
// ячейке — щелчок, в конце выигрышная ячейка встаёт ровно под золотую метку и
// подсвечивается. Ширина ячейки берётся из вёрстки (.roulette-item), поэтому
// остановка всегда точная — раньше считалось «на глаз» и могло не совпасть.
const ROULETTE_ITEM_COUNT = 90;
const ROULETTE_SPIN_MS = 6200;
const ROULETTE_MIN_TICK_MS = 45;
const ROULETTE_ITEM_FALLBACK = 84;

function chestRouletteFiller(isGold) {
    // Соседние ячейки показывают реальный набор наград, чтобы рулетка не врала:
    // капсула — 10 % в золотом сундуке и 0.1 % в обычном (как на сервере).
    if (Math.random() < chestCapsuleChance(isGold)) return { kind: 'capsule', amount: 1 };
    if (Math.random() < (isGold ? 0.08 : 0.18)) return { kind: 'key', amount: 0 };
    const min = isGold ? 60 : 10, max = isGold ? 180 : 30;
    return { kind: 'coin', amount: Math.floor(Math.random() * (max - min + 1)) + min };
}

function buildChestRouletteTrack({ isGold, winner }) {
    const track = document.getElementById('roulette-track');
    if (!track) return null;
    const winItem = winner && winner.kind ? winner : { kind: 'coin', amount: 0 };
    // Выигрышная ячейка КАЖДЫЙ раз на новом месте (20..79 из 90 ячеек) — как в
    // казино. Раньше индекс был почти фиксированным, и полоса останавливалась
    // «в одном и том же месте». Края не берём, чтобы полосе было куда ехать.
    const winIndex = Math.floor(Math.random() * 60) + 20;
    const cells = [];
    for (let i = 0; i < ROULETTE_ITEM_COUNT; i++) {
        const item = i === winIndex ? winItem : chestRouletteFiller(isGold);
        const cls = item.kind === 'key' ? ' is-key' : (item.kind === 'capsule' ? ' is-capsule' : '');
        cells.push(
            `<div class="roulette-item${cls}"${i === winIndex ? ' data-win="1"' : ''}>` +
            `${chestRouletteItemHtml(item.kind, item.amount)}</div>`
        );
    }
    track.innerHTML = cells.join('');
    track.style.transition = 'none';
    track.style.transform = 'translateX(0)';
    return { track, winIndex };
}

// Крутит рулетку и резолвится, когда ячейка встала на место и подсвечена.
// Задача 70: «Skip». Награда уже известна (сервер её выдал до анимации),
// поэтому пропуск ничего не ломает: обрываем прокрут и сразу показываем награду.
let chestSkipRequested = false;
let chestRouletteActive = false;

function skipChestAnimation() {
    if (!chestRouletteActive) return;
    chestSkipRequested = true;
    const container = document.getElementById('roulette-container');
    if (container) { container.classList.remove('spinning'); container.classList.add('hit'); }
}

function runChestRoulette({ isGold, winner }) {
    return new Promise(resolve => {
        chestSkipRequested = false;
        chestRouletteActive = true;
        const container = document.getElementById('roulette-container');
        const built = buildChestRouletteTrack({ isGold, winner });
        if (!container || !built) return resolve(false);
        const { track, winIndex } = built;

        container.classList.remove('hit');
        container.classList.add('spinning');
        container.style.display = 'block';
        const caption = document.getElementById('roulette-caption');
        if (caption) caption.textContent = 'Открываем сундук…';
        void container.offsetWidth;             // фиксируем вёрстку до измерений

        const firstCell = track.firstElementChild;
        const itemWidth = (firstCell && firstCell.getBoundingClientRect().width) || ROULETTE_ITEM_FALLBACK;
        const windowEl = track.parentElement;
        const windowWidth = (windowEl && windowEl.getBoundingClientRect().width) || 340;
        // Центр выигрышной ячейки должен совпасть с центром окна.
        const target = Math.max(0, winIndex * itemWidth - (windowWidth / 2 - itemWidth / 2));

        let lastCell = -1, lastTickAt = 0;
        const startedAt = performance.now();
        const frame = now => {
            const progress = Math.min((now - startedAt) / ROULETTE_SPIN_MS, 1);
            const eased = 1 - Math.pow(1 - progress, 4);   // резкий разгон → долгое торможение
            const offset = target * eased;
            track.style.transform = `translateX(-${offset}px)`;

            const cell = Math.round((offset + windowWidth / 2) / itemWidth);
            if (cell !== lastCell) {
                lastCell = cell;
                if (now - lastTickAt > ROULETTE_MIN_TICK_MS) {
                    lastTickAt = now;
                    playSound('click');
                }
            }

            if (progress < 1 && !chestSkipRequested) { requestAnimationFrame(frame); return; }

            chestRouletteActive = false;
            track.style.transform = `translateX(-${target}px)`;
            container.classList.remove('spinning');
            container.classList.add('hit');
            const winner = track.querySelector('[data-win="1"]');
            if (winner) winner.classList.add('win', 'hit');
            if (caption) caption.textContent = 'Ваша награда!';
            try { tg.HapticFeedback && tg.HapticFeedback.notificationOccurred('success'); } catch (e) {}
            playSound('win');
            resolve(true);
        };
        requestAnimationFrame(frame);
    });
}

// Сундуки открыты всем, как только сервер снял техработы (can_play: true).
// Задача 67: сундуки открыты всем, как и вся игра. Сообщение оставили на случай,
// если сервер когда-нибудь ответит «нельзя» по другой причине (например, бан).
const CHEST_MAINTENANCE_ALERT = 'Сундуки сейчас недоступны. Попробуйте позже.';

function chestAccessGranted() {
    // Задача 71: во время техработ играть нельзя (кроме админа). Сервер всё
    // равно ответит 403, но лучше не отправлять запрос вовсе.
    return canPlay || isAdmin;
}

async function openChest(type) {
    if (isProcessing) return;
    if (!chestAccessGranted()) return tg.showAlert(CHEST_MAINTENANCE_ALERT);
    const isGold = type === 'gold';
    if (isGold && keys <= 0) return tg.showAlert("У вас нет ключей!");
    isProcessing = true;

    // 1) Вспышка и запрос к серверу идут одновременно: награда уже известна,
    //    но баланс ещё НЕ тронут.
    const flashPromise = playChestFlash(1000);
    let data = null;
    try {
        const response = await fetch(`${API_URL}/api/open_chest`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, chest_type: isGold ? 'gold' : 'normal',
                                   request_id: requestId() })
        });
        data = await response.json().catch(() => ({}));
        if (!response.ok) {
            isProcessing = false;
            const flashEl = document.getElementById('chest-flash');
            if (flashEl) flashEl.classList.remove('active');
            // 403 — техработы; 503 — не применена миграция chest_claims. Пишем
            // понятную причину, чтобы админ сразу видел, что чинить.
            if (response.status === 403) return tg.showAlert(CHEST_MAINTENANCE_ALERT);
            return tg.showAlert(data.detail || "Сундук перезаряжается!");
        }
    } catch (e) {
        isProcessing = false;
        document.getElementById('chest-flash').classList.remove('active');
        return tg.showAlert("Ошибка соединения");
    }

    // Ключ за попытку списан сразу, коины из награды — только после «Забрать».
    keys = Number(data.new_keys) || 0;
    if (data.last_chest) lastChestTime = Number(data.last_chest);
    const rewardCoins = Number(data.reward_coins) || 0;
    const rewardKeys = Number(data.reward_keys) || 0;
    const isKey = rewardKeys > 0;
    // Капсула (если выпала) — отдельная награда вдобавок к коинам/ключу.
    // Начисление всё равно только по «Забрать», здесь мы её лишь показываем.
    const rewardItem = String(data.reward_item || '');
    const rewardItemAmount = Number(data.reward_item_amount) || 0;
    const hasCapsule = rewardItem === CHEST_CAPSULE_ITEM && rewardItemAmount > 0;
    chestPendingItem = hasCapsule ? { itemId: rewardItem, amount: rewardItemAmount } : null;
    // Баланс меняется ТОЛЬКО по кнопке «Забрать». Сервер всегда присылает
    // claim_id: число (строка в chest_claims) или строку «mem-…» (награда ждёт
    // в памяти процесса, если таблицы нет). Никакого «начислили сразу».
    chestRewardCredited = data.credited === true;
    if (chestRewardCredited) {
        // Задача 60: НЕ «|| 0». Если сервер не прислал new_balance, старая
        // запись обнуляла баланс прямо на глазах у игрока (0 в шапке).
        const credited = Number(data.new_balance);
        if (Number.isFinite(credited)) { balance = credited; balanceKnown = true; }
    }
    // claim_id не приводим к Number: строковый id «mem-…» отправится как есть.
    pendingChestClaimId = data.claim_id != null ? data.claim_id : null;
    updateUI();
    isProcessing = false;

    document.getElementById('chest-buttons').style.display = 'none';
    const winModal = document.getElementById('chest-win-modal');
    if (winModal) winModal.classList.remove('active');

    // Вспышка длится ровно 1 секунду — и только после неё появляется рулетка.
    // В подсвеченной ячейке — главная награда: капсула, если она выпала.
    const winner = hasCapsule ? { kind: 'capsule', amount: rewardItemAmount }
        : (isKey ? { kind: 'key', amount: 0 } : { kind: 'coin', amount: rewardCoins });
    await flashPromise;
    // Задача 70: если игрок ушёл из аппки во время прокрута (вкладка скрыта),
    // не держим награду до кнопки — сервер зачислит её сам при следующем заходе
    // через /api/chest_pending_claim. Здесь просто не запускаем долгую анимацию.
    if (document.visibilityState === 'hidden') {
        pendingChestClaimId = data.claim_id != null ? data.claim_id : null;
        return;
    }
    await runChestRoulette({ isGold, winner });
    // Небольшая пауза: игрок должен увидеть, что ячейка встала под меткой и
    // подсветилась, а уже потом поверх открывается окно награды.
    await new Promise(r => setTimeout(r, 700));

    const iconEl = document.getElementById('chest-win-icon');
    const textEl = document.getElementById('chest-win-text');
    if (iconEl) {
        iconEl.innerHTML = hasCapsule
            ? `<img class="cap-img" src="${CHEST_CAPSULE_IMG}" alt="капсула">`
            : (isKey ? '🔑' : COIN_IMG);
    }
    if (textEl) {
        // Показываем ВСЁ, что лежит в сундуке: и капсулу, и коины/ключ.
        const parts = [];
        if (isKey) parts.push('🔑 Золотой ключ');
        if (rewardCoins > 0) parts.push(coinAmount(rewardCoins));
        if (hasCapsule) parts.push(chestCapsuleText(rewardItemAmount));
        textEl.innerHTML = parts.length
            ? `Вы получили ${parts.join(' и ')}`
            : `Вы получили ${coinAmount(rewardCoins)}`;
    }
    if (winModal) winModal.classList.add('active');
}

async function claimChestReward() {
    // Начисление награды — здесь, а не при открытии сундука.
    if (isProcessing) return;
    if (!chestAccessGranted()) return tg.showAlert(CHEST_MAINTENANCE_ALERT);
    const claimId = pendingChestClaimId;
    if (claimId == null) {
        // Сервер не выдал claim_id — забирать нечего, но и не врём про баланс.
        hideChestRoulette();
        return;
    }

    const btn = document.getElementById('chest-claim-btn');
    isProcessing = true;
    if (btn) { btn.disabled = true; btn.innerText = 'Забираем…'; }
    try {
        const res = await fetch(`${API_URL}/api/claim_chest`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, claim_id: claimId })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            // Окно не закрываем: игрок нажмёт «Забрать» ещё раз.
            tg.showAlert(data.detail || "Не удалось забрать награду");
            return;
        }
        // Задача 60: без «|| 0» — иначе отсутствие поля в ответе = 0 коинов.
        const chestBalance = Number(data.new_balance);
        if (Number.isFinite(chestBalance)) { balance = chestBalance; balanceKnown = true; }
        const chestKeys = Number(data.new_keys);
        if (Number.isFinite(chestKeys)) keys = chestKeys;
        // Капсула приходит в инвентаре ответа — обновляем сразу, чтобы её было
        // видно в профиле без перезагрузки.
        if (Array.isArray(data.inventory)) inventory = data.inventory;
        else if (data.reward_item && Number(data.reward_item_amount) > 0 && chestPendingItem) {
            for (let i = 0; i < chestPendingItem.amount; i++) inventory.push(chestPendingItem.itemId);
        }
        updateUI();
        playSound('win');
    } catch (e) {
        console.error("Ошибка начисления сундука", e);
        tg.showAlert("Ошибка соединения");
        return;
    } finally {
        isProcessing = false;
        if (btn) { btn.disabled = false; btn.innerText = 'Забрать'; }
    }
    hideChestRoulette();
}

// ================= МАГАЗИН =================
// Если сервер хоть раз ответил «тираж списать не удалось» (RLS / нет миграции),
// не показываем «Осталось: X из Y» до перезапуска приложения: число в базе в
// этот момент уже не соответствует проданному, и показывать его — врать.
let shopStockUnreliable = false;
// Распроданные предметы (приходят с сервера): витрина должна сказать
// «Всё распродано», а не «Магазин пуст» — иначе это выглядит как поломка.
let shopSoldOut = [];

// Задача 43: кэш витрины. Раньше магазин при каждом входе на вкладку заново
// показывал «Загрузка магазина…» и ждал сеть (на бесплатном Render — до
// нескольких секунд). Теперь последний ответ рисуется СРАЗУ, а сеть обновляет
// его в фоне: карточки «раскрываются» мгновенно.
const SHOP_CACHE_KEY = 'aura_shop_cache_v1';
const SHOP_CACHE_TTL_MS = 5 * 60 * 1000;

function readShopCache() {
    try {
        const raw = localStorage.getItem(SHOP_CACHE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || !parsed.items) return null;
        return parsed;
    } catch (e) { return null; }
}

function writeShopCache(payload) {
    try {
        localStorage.setItem(SHOP_CACHE_KEY, JSON.stringify({
            at: Date.now(), items: payload.items,
            sold_out: payload.sold_out || [], passive_rates: payload.passive_rates || null,
        }));
    } catch (e) { /* приватный режим — просто не кэшируем */ }
}

// Применяет ответ сервера (и кэш) к локальным данным магазина.
function applyShopPayload(data) {
    shopSoldOut = Array.isArray(data.sold_out) ? data.sold_out : [];
    if (data.passive_rates && typeof data.passive_rates === 'object') {
        passiveRates = Object.assign({}, passiveRates, data.passive_rates);
    }
    SHOP_ITEMS = {};
    (data.items || []).forEach(item => {
        SHOP_ITEMS[item.id] = {
            name: item.name,
            shortName: item.short_name,
            emoji: item.emoji,
            price: Number(item.price),
            shopImage: item.shop_image,
            inventoryImage: item.inventory_image,
            dailyLimit: item.daily_limit,
            unlimited: Boolean(item.unlimited),
            stock: item.unlimited || item.stock == null ? null : Number(item.stock),
            stockReady: !item.unlimited && item.stock_ready !== false && !shopStockUnreliable,
            stockTotal: item.unlimited ? null
                : ((item.stock_total != null ? item.stock_total : item.total_limit) == null
                    ? null
                    : Number(item.stock_total != null ? item.stock_total : item.total_limit))
        };
    });
    shopItemsLoaded = (data.items || []).length > 0;
}

async function loadShopItems() {
    // Мгновенная отрисовка из кэша: вкладка не «думает» ни секунды.
    if (!shopItemsLoaded) {
        const cached = readShopCache();
        if (cached) {
            applyShopPayload(cached);
            renderShop();
            updateUI();
        }
    }
    try {
        const response = await fetch(`${API_URL}/api/shop_items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Не удалось загрузить магазин");
        applyShopPayload(data);
        writeShopCache(data);
        shopItemsLoaded = true;
    } catch (e) {
        console.error("Ошибка загрузки магазина", e);
        shopItemsLoaded = false;
    }
    renderShop();
    updateUI();
}

// Сколько коинов в час приносит капсула — мелкая подпись под ценой.
// Ставки живут на сервере (PASSIVE_INCOME_RATES в api.py) и приходят в профиле.
function passiveIncomeLabel(itemId) {
    const rate = Number(passiveRates[itemId] || 0);
    if (!(rate > 0)) return '';
    const value = rate < 1 ? rate.toFixed(1) : formatCoins(Math.round(rate));
    return `<small class="shop-income">приносит ${value} ${COIN_IMG}/час</small>`;
}

function renderShop() {
    const grid = document.getElementById('shop-grid');
    if (!grid) return;
    const ids = Object.keys(SHOP_ITEMS);
    if (ids.length === 0) {
        const emptyText = !shopItemsLoaded ? "Магазин недоступен"
            : (shopSoldOut.length > 0 ? "Всё распродано" : "Магазин пуст");
        grid.innerHTML = `<div class="inv-empty">${emptyText}</div>`;
        return;
    }
    grid.innerHTML = ids.map(itemId => {
        const item = SHOP_ITEMS[itemId];
        // «Осталось: X из Y» мелким шрифтом под капсулой; при нуле карточку не рисуем.
        const hasStock = item.stock != null && item.stockTotal != null && item.stockReady !== false;
        if (hasStock && item.stock <= 0) return '';
        // Лимитов нет — про остаток не пишем ничего. Если лимиты вернут и остаток
        // неизвестен, вместо фиктивного «1 000 из 1 000» будет честная подпись.
        const stockText = item.unlimited ? '' : (hasStock
            ? `<small class="shop-stock${item.stock <= Math.max(1, Math.round(item.stockTotal * 0.1)) ? ' low' : ''}">Осталось: ${formatCoins(item.stock)} из ${formatCoins(item.stockTotal)}</small>`
            : (item.stockTotal != null ? `<small class="shop-stock muted">Остаток уточняется</small>` : ''));
        const image = shopImageFor(itemId, item);
        const art = image
            ? `<div class="shop-art"><img src="${image}" alt="${escapeHtml(item.name)}" loading="lazy"></div>`
            : `<div class="item-icon">${item.emoji || '💊'}</div>`;
        return `
            <article class="shop-item">
                ${art}
                <h3>${item.name}</h3>
                <p>${coinAmount(item.price)}</p>
                ${passiveIncomeLabel(itemId)}
                ${stockText}
                <button class="buy-btn" onclick="buyItem('${itemId}')">Купить</button>
            </article>
        `;
    }).join('');
    // Если сервер прямо говорит, что тираж не ведётся, карточки не должны
    // показывать «Осталось: 1 000 из 1 000» — это и было причиной ложного
    // «Товар распродан» после успешной на вид покупки.
}

async function buyItem(itemId) {
    if (isProcessing) return;
    const item = SHOP_ITEMS[itemId];
    if (!item) return tg.showAlert("Неизвестный товар");
    if (balance < item.price) return tg.showAlert("Недостаточно коинов!");
    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/buy_item`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, item_id: itemId, request_id: requestId() })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            tg.showAlert(data.detail || "Не удалось купить предмет");
            if (res.status === 400 || res.status === 403) loadShopItems();
            return;
        }
        // Задача 60: пустое поле в ответе раньше превращало баланс в undefined.
        const shopBalance = Number(data.new_balance);
        if (Number.isFinite(shopBalance)) { balance = shopBalance; balanceKnown = true; }
        if (Array.isArray(data.inventory)) inventory = data.inventory;
        updateUI();
        showWin("✅ Успешно куплено!");
        playSound('click');
        // Тираж уменьшился — сначала ставим остаток из ответа сервера (видно сразу,
        // даже если следующий запрос не дойдёт), потом перечитываем каталог целиком.
        // Сервер не смог списать тираж → числам в этой сессии больше не верим:
        // следующий каталог может прочитать старую строку и снова показать
        // «1 000 из 1 000» после покупки.
        if (!data.unlimited) {
            if (data.stock_ready === false) shopStockUnreliable = true;
            else if (data.stock_ready === true) shopStockUnreliable = false;
        }
        if (SHOP_ITEMS[itemId]) {
            if (data.stock != null) SHOP_ITEMS[itemId].stock = Number(data.stock);
            SHOP_ITEMS[itemId].stockReady = data.stock_ready !== false && !shopStockUnreliable;
            renderShop();
        }
        if (data.stock_warning) console.warn('Магазин:', data.stock_warning);
        loadShopItems();
    } catch(e) {
        console.error("Ошибка покупки", e);
        tg.showAlert("Ошибка соединения");
    } finally {
        isProcessing = false;
    }
}

// ================= АПГРЕЙД (TONGRADER style) =================
function upgradePrice(itemId) {
    const item = SHOP_ITEMS[itemId];
    const price = Number(item && item.price);
    return Number.isFinite(price) && price > 0 ? price : (UPGRADE_FALLBACK_PRICES[itemId] || 500);
}

const UPGRADE_ITEM_NAMES = {
    capsule: 'Обычная капсула', uniq_capsule: 'Уникальная капсула', ruby: 'Рубиновая капсула',
    diamond: 'Алмазная капсула', galactic: 'Галактическая капсула',
    malachite: 'Малахитовая капсула', emerald: 'Изумрудная капсула',
};

function upgradeItemName(itemId) {
    const item = SHOP_ITEMS[itemId];
    if (item && item.name) return item.name;
    if (UPGRADE_ITEM_NAMES[itemId]) return UPGRADE_ITEM_NAMES[itemId];
    return 'Коины';
}

function upgradeItemEmoji(itemId) {
    const item = SHOP_ITEMS[itemId];
    if (item && item.emoji) return item.emoji;
    const found = UPGRADE_ITEM_CATALOG.find(entry => entry.id === itemId);
    if (found) return found.emoji;
    return itemId === 'coin' ? '💰' : '💊';
}

function formatCoins(value) {
    const number = Math.max(0, Math.round(Number(value) || 0));
    return number.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

function countItem(itemId) { return inventory.filter(item => item === itemId).length; }

function upgradeSourceValue() {
    const amount = Math.max(0, Number(upgradeFromAmount) || 0);
    return upgradeFrom === 'coin' ? Math.round(amount) : upgradePrice(upgradeFrom) * Math.round(amount);
}

function upgradeTargetValue(fromValue) {
    const base = fromValue === undefined ? upgradeSourceValue() : fromValue;
    if (upgradeTarget.kind === 'item') return upgradePrice(upgradeTarget.value);
    if (upgradeTarget.kind === 'chance') {
        const chance = Number(upgradeTarget.value) / 100;
        return chance > 0 ? base / chance : 0;
    }
    return base * Number(upgradeTarget.value);
}

// ================= ШАНС: ФИКСИРОВАННАЯ ТАБЛИЦА =================
// Шанс = 90 / X — ОДНА формула и для кнопок, и для «своего» множителя.
// Честный шанс был бы 100 / X; 90 / X — те же 10% в пользу игры, но игрок
// может проверить любое число на калькуляторе: x1.75 → 51.43%, x3.7 → 24.32%.
// Знаменатель приходит с сервера (economy.upgrade.chance_numerator).
const UPGRADE_MULT_CHANCES = { 1.12: 80.36, 1.35: 66.67, 1.55: 58.06, 1.75: 51.43, 20: 4.5, 50: 1.8 };
const UPGRADE_MULT_MIN = 1.12;
const UPGRADE_MULT_MAX = 50;
// Дом-эдж и админский множитель приходят с сервера (/api/user → economy.upgrade).
let upgradeEconomy = { house_edge: 1, chance_factor: 1, numerator: 90 };

function upgradeMultChance(mult) {
    const value = Number(mult);
    if (!Number.isFinite(value) || value <= 0) return 0;
    const numerator = Number(upgradeEconomy.numerator) > 0 ? Number(upgradeEconomy.numerator) : 90;
    // 90 / X, с тем же зажимом, что на сервере (0.5…95%).
    return Math.round(Math.max(0.5, Math.min(95, numerator / value)) * 100) / 100;
}

// Сырой шанс: для множителя — 90/X, для «цели по проценту» и капсул —
// отношение стоимостей (там нет сетки множителей).
function upgradeRawChance(fromValue, toValue) {
    if (upgradeTarget.kind === 'mult' && upgradeTarget.value !== undefined) {
        const fixed = upgradeMultChance(upgradeTarget.value);
        if (fixed > 0) return fixed;
    }
    if (!(fromValue > 0) || !(toValue > 0)) return 0;
    return (fromValue / toValue) * 100;
}

function upgradeChanceFor(fromValue, toValue) {
    return Math.min(UPGRADE_MAX_CHANCE, upgradeRawChance(fromValue, toValue));
}

function upgradeChance() { return upgradeRawChance(upgradeSourceValue(), upgradeTargetValue()); }

// ================= ДОМ-ЭДЖ =================
// Числа приходят с сервера и применяются ко ВСЕМ (больше нет «с баланса 1000
// играем хуже»): круг обязан показывать ровно тот шанс, по которому решает
// сервер, иначе игрок видит не то, что произошло.
function upgradeHouseEdgeFactor() {
    const edge = Number(upgradeEconomy.house_edge);
    return Number.isFinite(edge) && edge > 0 ? edge : 1;
}

function upgradeChanceFactor() {
    const factor = Number(upgradeEconomy.chance_factor);
    return Number.isFinite(factor) && factor > 0 ? factor : 1;
}

// Шанс, по которому реально играем (и который рисуем на круге).
function upgradeDisplayChance(raw) {
    const base = raw === undefined ? upgradeChance() : Number(raw);
    if (!Number.isFinite(base)) return 0;
    const value = base * upgradeHouseEdgeFactor() * upgradeChanceFactor();
    return Math.max(0, Math.min(100, Math.round(value * 100) / 100));
}

function upgradeChanceIsValid(chance) {
    return chance >= 0.5 && chance <= UPGRADE_MAX_CHANCE;
}

function upgradeRewardText(fromValue) {
    const base = fromValue === undefined ? upgradeSourceValue() : fromValue;
    if (upgradeTarget.kind === 'item') {
        return `${upgradeItemEmoji(upgradeTarget.value)} ${coinAmount(upgradePrice(upgradeTarget.value))}`;
    }
    return `${coinAmount(upgradeTargetValue(base))}`;
}

function upgradeOwned(itemId) { return itemId === 'coin' ? balance : countItem(itemId); }

function upgradeAmountLimits() {
    if (upgradeFrom === 'coin') return { min: UPGRADE_MIN_COINS, max: UPGRADE_MAX_COINS, step: 50 };
    const owned = Math.max(1, countItem(upgradeFrom));
    return { min: 1, max: Math.min(UPGRADE_MAX_CAPSULES, owned), step: 1 };
}

function upgradeCanStart() {
    const value = upgradeSourceValue();
    if (value <= 0) return false;
    if (upgradeFrom === 'coin') {
        if (upgradeFromAmount < UPGRADE_MIN_COINS || upgradeFromAmount > UPGRADE_MAX_COINS) return false;
        if (balance < upgradeFromAmount) return false;
    } else if (countItem(upgradeFrom) < upgradeFromAmount) {
        return false;
    }
    // Задача 54: шанс и «ХОЧУ» не могут быть «невалидными» — кнопка всегда живая.
    // Раньше при выбранном множителе, для которого UPGRADE_MULT_CHANCES не
    // содержит ключа (например свой X), кнопка молча гасла и выглядела как
    // «нажал — ничего не произошло, ставка не списалась».
    return true;
}

// Если капсул выбранного типа не осталось (например, после апгрейда) — уходим на коины.
function ensureUpgradeSourceValid() {
    // Пока идёт раунд, форма заморожена: ставка уже списана (и показана
    // списанной), поэтому нельзя «уводить» источник на коины или уменьшать
    // количество — подпись «Выигрыш» и круг разошлись бы с тем, что реально
    // ушло на сервер. После раунда renderUpgrade всё поправит сам.
    if (upgradeBusy) return;
    if (upgradeFrom === 'coin') {
        upgradeFromAmount = Math.min(UPGRADE_MAX_COINS, Math.max(upgradeFromAmount, UPGRADE_MIN_COINS));
        return;
    }
    const owned = countItem(upgradeFrom);
    if (owned <= 0) {
        upgradeFrom = 'coin';
        upgradeFromAmount = Math.min(UPGRADE_MAX_COINS, Math.max(upgradeFromAmount, UPGRADE_MIN_COINS));
        document.querySelectorAll('.up-from-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.from === 'coin'));
        return;
    }
    upgradeFromAmount = Math.min(owned, UPGRADE_MAX_CAPSULES, Math.max(1, upgradeFromAmount));
}

function lockUpgradeChance(value) {
    const number = Number(value);
    upgradePlacedChance = Number.isFinite(number) && number > 0 ? number : null;
    // Фиксируем границу круга СРАЗУ, тем же числом, по которому сервер считал
    // вердикт: иначе зелёная зона какое-то время показывала бы локальный расчёт,
    // а стрелка останавливалась бы по серверному — расхождение на границе.
    if (upgradePlacedChance !== null) setUpgradeRingPercent(upgradePlacedChance);
}

function releaseUpgradeChance() {
    upgradePlacedChance = null;
}

function upgradeRingChance() {
    // Пока раунд не сыгран — показываем боевой шанс (с дом-эджем), чтобы круг
    // не «сжимался» в момент остановки стрелки.
    return upgradePlacedChance === null ? upgradeDisplayChance() : upgradePlacedChance;
}

// ОДНА формула угла на весь круг: и заливка (--p), и белая риска границы,
// и стрелка считают угол только отсюда. 0% — верх круга, дальше по часовой.
const UPGRADE_DEG_PER_PERCENT = 3.6;

function upgradeAngleForPercent(percent) {
    const value = Number(percent);
    const safe = Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
    return safe * UPGRADE_DEG_PER_PERCENT;
}

// Ставит --p круга (границу зелёной зоны) по проценту.
// Дополнительно заливаем круг ИЗ JS тем же углом и явным `from 0deg`:
// так зона не зависит от того, какая версия style.css прилетела в кэш.
// Раньше в CSS стояло `from -90deg`, и круг был повёрнут на 90°: при шансе
// 50% зелёным оказывался ВЕРХ, а стрелка считала угол от верха — отсюда
// «на красном зашло / на зелёном не может».
function upgradeRingGradientCss(percent) {
    const circle = document.getElementById('up-circle');
    const win = !!circle && circle.classList.contains('win');
    const lose = !!circle && circle.classList.contains('lose');
    const deg = upgradeAngleForPercent(percent).toFixed(2) + 'deg';
    const green = lose ? 'rgba(34, 197, 94, 0.45)' : '#22c55e';
    const red = win ? 'rgba(239, 68, 68, 0.3)' : '#ef4444';
    return `conic-gradient(from 0deg, ${green} 0 ${deg}, ${red} ${deg} 360deg)`;
}

function setUpgradeRingGradient(percent) {
    const ring = document.getElementById('up-circle-ring');
    if (!ring) return;
    ring.style.background = upgradeRingGradientCss(percent);
}

function setUpgradeRingPercent(percent) {
    const circle = document.getElementById('up-circle');
    if (!circle) return;
    circle.style.setProperty('--p', upgradeAngleForPercent(percent).toFixed(2) + 'deg');
    setUpgradeRingGradient(percent);
}

// Выигрыш = бросок НЕ БОЛЬШЕ шанса (шанс 50%: бросок 22 — победа, 50 — тоже победа,
// 50.01 — проигрыш). Ровно то же правило на сервере: roll <= chance — победа.
// Функция одна на клиент, чтобы стрелка, дуга и текст не могли разойтись:
// зелёная зона на круге — это [0, шанс], и стрелка стоит на upgradeAngleForPercent(roll).
function upgradeRollWon(roll, chance) {
    // null/undefined/'' — это «броска нет», а не 0: Number(null) === 0 иначе
    // превращал отсутствие данных в «победу с шансом 0%».
    const isBlank = v => v === null || v === undefined || v === '';
    if (isBlank(roll) || isBlank(chance)) return null;
    const rollNumber = Number(roll);
    const chanceNumber = Number(chance);
    if (!Number.isFinite(rollNumber) || !Number.isFinite(chanceNumber)) return null;
    return rollNumber <= chanceNumber;
}

// Попал ли бросок в зелёную зону — для логов и проверки честности.
function upgradeRollInsideSector(roll, chance) {
    const won = upgradeRollWon(roll, chance);
    if (won === null) return null;
    const angle = upgradeAngleForPercent(roll);
    const sector = upgradeAngleForPercent(chance);
    // Зелёная зона — [0, шанс], граница включена: ровно так же, как считает
    // сервер (roll <= chance — победа) и как залит conic-gradient.
    return angle <= sector + 1e-9;
}

// Каким шансом реально играли. Сервер присылает и визуальный chance, и real_chance
// (для админов они совпадают): вердикт считается по real_chance, поэтому клиент
// обязан сверяться именно с ним, иначе «попал в сектор, а написали проигрыш».
function upgradeEffectiveChance(data, fallback) {
    const real = Number(data && data.real_chance);
    if (Number.isFinite(real) && real > 0) return real;
    const visual = Number(data && data.chance);
    if (Number.isFinite(visual) && visual > 0) return visual;
    const local = Number(fallback);
    return Number.isFinite(local) ? local : 0;
}

// Итог раунда крупной цифрой ПОД кругом. Элемент обычный (в потоке),
// поэтому он не может наехать на круг и на «ШАНС УСПЕХА».
function setUpgradeNet(html, state) {
    const el = document.getElementById('up-net');
    if (!el) return;
    // Пустое состояние — просто пустая строка: высота зарезервирована в CSS,
    // поэтому вёрстка не прыгает между раундами.
    el.className = 'up-net' + (state ? ' ' + state : '');
    el.innerHTML = html || '';
}

function clearUpgradeNet() {
    setUpgradeNet('', '');
}

function setUpgradeNetFromResult(won, data) {
    if (!data) { clearUpgradeNet(); return; }
    const emoji = (id) => upgradeItemEmoji(id);
    if (won) {
        const win = data.win_item === 'coin'
            ? `+${coinAmount(data.win_amount)}`
            : `+${Number(data.win_amount) || 1} ${emoji(data.win_item)}`;
        setUpgradeNet(win, 'win');
    } else {
        const lost = (data.item_from || upgradeFrom) === 'coin'
            ? `−${coinAmount(data.amount_from)}`
            : `−${Number(data.amount_from) || 0} ${emoji(data.item_from || upgradeFrom)}`;
        setUpgradeNet(lost, 'lose');
    }
}

function setUpgradeResult(text, state) {
    upgradeResultState = state || '';
    const el = document.getElementById('upgrade-result');
    if (!el) return;
    // innerHTML: в тексте результата встречается картинка монеты (coinAmount).
    // С innerText игрок видел сырой HTML вместо награды.
    el.innerHTML = text;
    // 'error' — техническая ошибка (не проигрыш): красный текст, но кнопка
    // остаётся «АПГРЕЙД», чтобы игрок не думал, что ставка сгорела.
    el.className = 'up-result' + (state ? ' ' + (state === 'error' ? 'lose' : state) : '');
}

// Возвращает круг и кнопку в исходное состояние: можно сразу ставить снова.
function resetUpgradeRound() {
    upgradeAwaitingRestart = false;
    upgradeSpinToken++;          // отменяем любые недоигранные анимации стрелки
    upgradeFetchPending = false;
    if (upgradeBusyTimer) { clearTimeout(upgradeBusyTimer); upgradeBusyTimer = null; }
    upgradeBusy = false;         // после сброса кнопка обязана быть живой
    stopUpgradeArrow();
    resetUpgradeArrow();
    releaseUpgradeChance();
    const circle = document.getElementById('up-circle');
    if (circle) {
        circle.classList.remove('spinning', 'win', 'lose');
        // Дуга снова показывает шанс текущей формы (с дом-эджем), а не прошлого раунда.
        setUpgradeRingPercent(upgradeDisplayChance());
    }
    const btn = document.getElementById('upgrade-btn');
    if (btn) {
        btn.disabled = false;
        btn.classList.remove('win', 'lose');
        btn.innerText = 'АПГРЕЙД';       // синий цвет — базовый стиль кнопки
    }
    setUpgradeResult('Выбери предмет и цель', '');
    clearUpgradeNet();
    renderUpgrade();
}

// Снимаем «крутится» и возвращаем кнопке рабочее состояние.
// Нужно на всех путях, где раунд мог прерваться (перезаход, ошибка, таймаут):
// иначе upgradeBusy остаётся true и кнопка не реагирует до перезагрузки аппки.
function releaseUpgradeBusy() {
    upgradeBusy = false;
    // Задача 60: раунда больше нет — снимаем и «потолок» баланса, иначе он
    // переживёт раунд и будет резать баланс на каждом обновлении профиля.
    if (!upgradeFetchPending) clearUpgradeBalanceLock();
    if (upgradeBusyTimer) { clearTimeout(upgradeBusyTimer); upgradeBusyTimer = null; }
    const circle = document.getElementById('up-circle');
    if (circle) circle.classList.remove('spinning');
    const btn = document.getElementById('upgrade-btn');
    if (btn) {
        btn.disabled = false;
        btn.classList.remove('win', 'lose');
        if (btn.innerText === 'КРУТИМ...') btn.innerText = 'АПГРЕЙД';
    }
}

// Сторож: если по какой-то причине ответ/анимация потерялись, через 15 секунд
// кнопка всё равно оживает. Лучше показать «АПГРЕЙД», чем мёртвую кнопку.
function armUpgradeBusyWatchdog() {
    if (upgradeBusyTimer) clearTimeout(upgradeBusyTimer);
    upgradeBusyTimer = setTimeout(() => {
        upgradeBusyTimer = null;
        if (!upgradeBusy) return;
        console.warn('Апгрейд: раунд не завершился за 15 с — разблокируем кнопку');
        releaseUpgradeBusy();
        renderUpgrade();
    }, 15000);
}

// Единая точка входа для кнопки: либо сброс после раунда, либо новая ставка.
function onUpgradeButtonClick() {
    if (upgradeBusy) {
        // Страховка от «залипшей» кнопки: если спин давно не идёт и запрос не ждём —
        // считаем раунд потерянным и оживляем кнопку, а не игнорируем клик молча.
        if (upgradeArrowRaf === null && !upgradeFetchPending) releaseUpgradeBusy();
        else return;
    }
    if (upgradeAwaitingRestart) return resetUpgradeRound();
    return startUpgrade();
}

function clearUpgradeResult() {
    // Любое изменение формы = новый раунд: снимаем фиксацию прошлого шанса.
    releaseUpgradeChance();
    const circle = document.getElementById('up-circle');
    if (circle) circle.classList.remove('win', 'lose');
    if (!upgradeBusy) {
        stopUpgradeArrow();
        resetUpgradeArrow();
    }
    upgradeAwaitingRestart = false;
    const btn = document.getElementById('upgrade-btn');
    if (btn) { btn.disabled = false; btn.classList.remove('win', 'lose'); btn.innerText = 'АПГРЕЙД'; }
    setUpgradeResult('Выбери предмет и цель', '');
    clearUpgradeNet();
}

// Вкладки «Инвентарь»: коины + каждая капсула, которая вообще бывает в игре.
// Показываем счётчик, пустые капсулы приглушаем, но не прячем — так игрок
// видит, что рубиновую/алмазную тоже можно поставить.
// ================= СВОЙ МНОЖИТЕЛЬ (ползунок) =================
// Шкала логарифмическая: иначе на линейном отрезке 1.12…50 весь «интересный»
// диапазон (1.12–2) съедался бы первыми тремя процентами ползунка.
function upgradeMultFromSlider(raw) {
    const min = Math.log(UPGRADE_MULT_MIN);
    const max = Math.log(UPGRADE_MULT_MAX);
    const ratio = Math.max(0, Math.min(1000, Number(raw) || 0)) / 1000;
    const value = Math.exp(min + (max - min) * ratio);
    return Math.max(UPGRADE_MULT_MIN, Math.min(UPGRADE_MULT_MAX, Math.round(value * 100) / 100));
}

function upgradeSliderFromMult(mult) {
    const min = Math.log(UPGRADE_MULT_MIN);
    const max = Math.log(UPGRADE_MULT_MAX);
    const value = Math.max(UPGRADE_MULT_MIN, Math.min(UPGRADE_MULT_MAX, Number(mult) || UPGRADE_MULT_MIN));
    return Math.round((Math.log(value) - min) / (max - min) * 1000);
}

// Задача 52: ползунок больше не переписывает цель «на каждый пиксель» — иначе
// подсветка прыгала на любое значение.
// Задача 56: сетку целей убрали, поэтому ползунок — это И ЕСТЬ выбор X.
// Двигаешь — применяется сразу (через паузу), отпускаешь — применяется точно.
// Раньше после удаления сетки кнопка АПГРЕЙД играла бы старую цель: игрок
// видел x2.5 на экране, а ставка уходила на x1.35.
function syncUpgradeCustomMult() {
    const slider = document.getElementById('up-custom-range');
    const label = document.getElementById('up-custom-value');
    const chanceEl = document.getElementById('up-custom-chance');
    const isMult = upgradeTarget.kind === 'mult';
    const mult = isMult ? Number(upgradeTarget.value) : upgradeMultFromSlider(slider ? slider.value : 0);
    if (slider && isMult) slider.value = String(upgradeSliderFromMult(mult));
    if (label) label.textContent = `x${Number(mult).toFixed(2)}`;
    if (chanceEl) chanceEl.textContent = `${upgradeDisplayChance(upgradeMultChance(mult)).toFixed(2)}%`;
}

// Ползунок двигают — обновляем цифры сразу (без полной перерисовки, чтобы
// палец не «дёргался»). Полная перерисовка — по окончании движения.
let upgradeCustomApplyTimer = null;

function onCustomMultInput(raw) {
    if (upgradeBusy) return;
    const mult = upgradeMultFromSlider(raw);
    const valueLabel = document.getElementById('up-custom-value');
    if (valueLabel) valueLabel.textContent = `x${mult.toFixed(2)}`;
    const chanceEl = document.getElementById('up-custom-chance');
    if (chanceEl) chanceEl.textContent = `${upgradeDisplayChance(upgradeMultChance(mult)).toFixed(2)}%`;
    // Задача 56: применяем X с небольшой задержкой, чтобы цифры на экране и
    // реальная цель совпадали. Палец ведёт ползунок — цель не дёргаем.
    if (upgradeCustomApplyTimer) clearTimeout(upgradeCustomApplyTimer);
    upgradeCustomApplyTimer = setTimeout(() => {
        upgradeCustomApplyTimer = null;
        applyCustomMult();
    }, 220);
}

// Применить свой X: ползунок (после паузы/отпускания) или пресет.
function applyCustomMult(raw) {
    if (upgradeBusy) return;
    const slider = document.getElementById('up-custom-range');
    const mult = upgradeMultFromSlider(raw === undefined && slider ? slider.value : raw);
    const same = upgradeTarget.kind === 'mult'
        && Math.abs(Number(upgradeTarget.value) - mult) < 0.005;
    upgradeToTab = 'coin';
    upgradeTarget = { kind: 'mult', value: mult };
    if (same) return;                     // ничего не изменилось — не мигаем
    clearUpgradeResult();
    playSound('click');
    renderUpgrade();
}

// Отпустили ползунок — применяем сразу, без ожидания таймера.
function onCustomMultCommit() {
    if (upgradeBusy) return;
    if (upgradeCustomApplyTimer) { clearTimeout(upgradeCustomApplyTimer); upgradeCustomApplyTimer = null; }
    playSound('click');
    applyCustomMult();
}

function renderUpgradeFromTabs() {
    const box = document.getElementById('up-from-tabs');
    if (!box) return;
    // Задача 45: полоса «🎒 Инвентарь» снова показывается — без неё нельзя
    // поставить в апгрейд капсулу (игрок просил вернуть, а убирать надо было
    // выбор ПРЕДМЕТА-ЦЕЛИ).
    if (box.hidden) { box.hidden = false; box.style.display = ''; }
    const title = document.getElementById('up-from-title');
    if (title) { title.hidden = false; title.style.display = ''; }
    const tabs = [{
        id: 'coin',
        label: 'Коины',
        emoji: `<img class="coin-img" src="coin.png" alt="монета">`,
        count: null,
    }].concat(UPGRADE_ITEM_CATALOG.map(item => ({
        id: item.id,
        label: item.short,
        emoji: item.emoji,
        count: countItem(item.id),
    })));

    box.innerHTML = tabs.map(tab => {
        const active = upgradeFrom === tab.id ? ' active' : '';
        const empty = tab.count === 0 ? ' empty' : '';
        const count = tab.count === null ? '' : ` <b>${tab.count}</b>`;
        return `<button type="button" class="up-from-tab${active}${empty}" data-from="${tab.id}"
                        onclick="selectUpgradeFrom('${tab.id}', this)">
                    <span class="up-from-emoji">${tab.emoji}</span>
                    <span class="up-from-label">${tab.label}${count}</span>
                </button>`;
    }).join('');
}

function renderUpgrade() {
    // innerHTML: в текстах встречается картинка монеты.
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.innerHTML = value; };

    ensureUpgradeSourceValid();

    // --- левая панель ---
    const fromValue = upgradeSourceValue();
    if (document.getElementById('up-from-icon')) {
        const icon = document.getElementById('up-from-icon');
        icon.innerHTML = upgradeFrom === 'coin' ? COIN_IMG : upgradeItemEmoji(upgradeFrom);
    }
    setText('up-from-name', upgradeFrom === 'coin' ? 'Коины' : upgradeItemName(upgradeFrom));
    setText('up-from-value', upgradeFrom === 'coin'
        ? coinAmount(upgradeFromAmount)
        : `${upgradeFromAmount} шт. · ${coinAmount(fromValue)}`);
    setText('up-from-tag', upgradeFrom === 'coin' ? 'ставка' : 'кол-во');

    renderUpgradeFromTabs();

    // --- поле ввода и ползунок ---
    const limits = upgradeAmountLimits();
    const amountInput = document.getElementById('upgrade-amount');
    if (amountInput) {
        amountInput.min = limits.min; amountInput.max = limits.max; amountInput.step = limits.step;
        if (String(amountInput.value) !== String(upgradeFromAmount)) amountInput.value = upgradeFromAmount;
    }
    const slider = document.getElementById('upgrade-slider');
    if (slider) {
        slider.min = limits.min; slider.max = limits.max; slider.step = limits.step;
        if (String(slider.value) !== String(upgradeFromAmount)) slider.value = upgradeFromAmount;
    }
    upgradeToTab = upgradeFrom === 'coin' ? 'coin' : 'capsule';
    setText('up-amount-label', upgradeFrom === 'coin' ? 'Сколько ставим?' : 'Сколько капсул?');
    setText('up-amount-min', upgradeFrom === 'coin' ? formatCoins(limits.min) : String(limits.min));
    setText('up-amount-max', upgradeFrom === 'coin' ? formatCoins(limits.max) : String(limits.max));

    // --- круг и шанс ---
    // Если раунд уже сыгран, показываем шанс, с которым он играл (upgradePlacedChance),
    // а не пересчитанный из формы — иначе дуга врёт о результате.
    const liveChance = upgradeChance();
    const chance = upgradeRingChance();
    const circle = document.getElementById('up-circle');
    if (circle) {
        setUpgradeRingPercent(chance);
        circle.classList.toggle('invalid', upgradePlacedChance === null && !upgradeChanceIsValid(liveChance));
    }
    if (!upgradeBusy) setText('up-chance', chance.toFixed(2) + '%');
    // Задача 64: когда шанс вне допустимого диапазона, под кольцом появляется
    // строка-объяснение (раньше текст ошибки прилетал поверх кольца и
    // накладывался на цифру). Надпись живёт в обычном блоке в потоке.
    if (!upgradeBusy && upgradePlacedChance === null && !upgradeChanceIsValid(liveChance)) {
        // Строка короткая: блок под кольцом рассчитан на одну-две строки, длинный
        // текст распирал экран и наезжал на кнопку «Закрыть» у подсказки.
        const why = liveChance > UPGRADE_MAX_CHANCE
            ? `Шанс ${liveChance.toFixed(2)}% — выбери цель подороже`
            : `Шанс ${liveChance.toFixed(2)}% — выбери цель подешевле`;
        if (!upgradeResultState || upgradeResultState === 'error') setUpgradeResult(why, 'error');
    }
    const outcome = document.getElementById('up-outcome');
    if (outcome) {
        outcome.innerHTML = `Выигрыш: <b>${upgradeRewardText()}</b>`;
    }

    // --- отладочная подпись формулы (контейнер скрыт в разметке) ---
    const hint = document.getElementById('up-to-hint');
    if (hint) {
        if (upgradeToTab === 'coin') {
            // Формула одна и та же для кнопок и для своего X — показываем её
            // прямо в подписи, чтобы не было вопросов «почему 45.71%».
            hint.textContent = `Шанс = ${Number(upgradeEconomy.numerator) || 90} / X · `
                + UPGRADE_MULTS.map(mult => {
                    const value = upgradeDisplayChance(upgradeMultChance(mult));
                    return `x${mult} → ${value.toFixed(2)}%`;
                }).join(' · ');
        } else {
            hint.textContent = 'Шанс = стоимость вашего предмета / стоимость цели';
        }
    }
    renderUpgradeTargets();
    // Задача 51: ползунок синхронизируем ПОСЛЕ нормализации цели. Раньше
    // syncUpgradeCustomMult() вызывался выше, когда upgradeTarget ещё мог быть
    // item, и в «Своём множителе» навсегда оставалось «выбери X» (баг со скрина).
    syncUpgradeCustomMult();

    // Задача 52: кнопка «ПРИМЕНИТЬ» в карточке своего множителя неактивна,
    // когда капсулы (цель — предмет, множитель тут не применим).
    const applyBtn = document.querySelector('.up-custom-apply');
    if (applyBtn) applyBtn.disabled = upgradeFrom !== 'coin';

    // --- кнопка ---
    // После результата кнопка снова синяя и активная: следующий раунд
    // запускается тем же кликом, без выхода из игры. Блокируем её только
    // пока крутится или пока ставка невозможна (нет денег/капсул).
    const btn = document.getElementById('upgrade-btn');
    if (btn) {
        btn.classList.remove('win', 'lose');
        if (upgradeBusy) {
            btn.disabled = true;
            btn.innerText = 'КРУТИМ...';
        } else {
            // Результат прошлого раунда остаётся текстом ниже, а кнопка уже
            // зовёт играть снова — без перезахода в игру.
            const hasResult = upgradeAwaitingRestart
                && (upgradeResultState === 'win' || upgradeResultState === 'lose');
            // «Играть заново» доступна всегда: клик только сбрасывает круг,
            // даже если денег на новую ставку уже нет — иначе кнопка «залипает».
            btn.disabled = hasResult ? false : !upgradeCanStart();
            btn.innerText = hasResult ? 'ИГРАТЬ ЗАНОВО' : 'АПГРЕЙД';
        }
    }
    updateUpgradeStatsUI();
}

function renderUpgradeTargets() {
    const grid = document.getElementById('up-to-grid');

    // Сначала приводим цель к источнику — ДО того, как решаем, что рисовать.
    // Раньше при ставке капсулой здесь оставался множитель, и проверка ниже
    // «цель — предмет?» давала false: сетка целей пряталась, а цель молча
    // переписывалась на «обычную капсулу» уже после отрисовки — отсюда 100.00%
    // на круге и «выбери цель подороже» без единой цели на экране.
    if (upgradeFrom === 'coin') {
        upgradeCapsuleCoinTarget = false;
        if (upgradeTarget.kind === 'item') {
            // Задача 52: возвращаемся к цели из сетки, а не к «x2», которой там нет.
            upgradeTarget = { kind: 'mult', value: UPGRADE_DEFAULT_MULT };
        }
    } else {
        const sourceValue = upgradeSourceValue();
        // Цель-предмет годится, только если она СТРОГО дороже ставки: иначе
        // шанс 100% и сервер отвечает «выбери цель подороже» (скрин заказчика).
        if (upgradeTarget.kind === 'item'
            && upgradePrice(upgradeTarget.value) > sourceValue) {
            upgradeCapsuleCoinTarget = false;
        } else if (upgradeTarget.kind === 'mult' && upgradeCapsuleCoinTarget) {
            // Капсула → коины: множитель уже выбран игроком, не трогаем.
        } else {
            // Задача 65: цель по умолчанию — ближайшая ДОРОЖЕ ставки. Нет такой
            // (галактическая капсула) — уходим в коины, а не в заведомые 100%.
            const dearer = upgradePickDearerTarget();
            if (dearer) {
                upgradeTarget = { kind: 'item', value: dearer };
                upgradeCapsuleCoinTarget = false;
            } else {
                upgradeTarget = { kind: 'mult', value: UPGRADE_DEFAULT_MULT };
                upgradeCapsuleCoinTarget = true;
            }
        }
    }

    // Задача 65: при ставке КОИНАМИ цель — множитель (ползунок и чипы), сетка
    // скрыта (задача 56). При ставке КАПСУЛОЙ цель выбирается карточками
    // предметов: без этого шанс всегда был 100%, а «выбери цель подороже»
    // предлагало то, чего на экране нет.
    upgradeItemGridVisible = upgradeFrom !== 'coin';
    renderUpgradeItemTargets();
    renderUpgradePresets();
    // Дальше по функции — старая отрисовка в скрытую сетку: она нужна только
    // коинам (множители/своя цель), при капсулах выходим сразу.
    if (upgradeItemGridVisible) return;
    if (!grid) return;
    // Ползунок всегда стоит на выбранном множителе: плитка «СВОЙ X» и плашка
    // под сеткой показывают одно и то же число (задача 52).
    const customSlider = document.getElementById('up-custom-range');
    if (customSlider && upgradeTarget.kind === 'mult') {
        customSlider.value = String(upgradeSliderFromMult(Number(upgradeTarget.value)));
    }
    const fromValue = upgradeSourceValue();
    const tiles = [];

    // Задача 45: цель зависит от ИСТОЧНИКА. Ставим коины — цель выбирается
    // множителем (x1.12 … x50). Ставим капсулу — цель выбирается предметом
    // (капсула → капсула и т.д.), иначе «капсулы нельзя получать».
    const targetMode = upgradeFrom === 'coin' ? 'coin' : 'item';

    if (targetMode === 'coin') {
        UPGRADE_MULTS.forEach(mult => {
            const toValue = fromValue * mult;
            // Шанс берём из таблицы «множитель → шанс», а не 1/множитель:
            // x1.75 теперь ровно 50%, а не 45.71%.
            const shownChance = upgradeDisplayChance(upgradeMultChance(mult));
            const chance = shownChance;
            const active = upgradeTarget.kind === 'mult' && Number(upgradeTarget.value) === mult;
            tiles.push({
                key: `mult:${mult}`,
                active,
                disabled: !upgradeChanceIsValid(chance),
                html: `<div class="up-to-tile-icon">${COIN_IMG}</div>
                       <div class="up-to-tile-name">x${mult}</div>
                       <div class="up-to-tile-value">${coinAmount(toValue)}</div>
                       <div class="up-to-tile-chance">${shownChance.toFixed(2)}%</div>`
            });
        });
        // --- своя цель: тот же вид плитки, что у x1.12…x50 ---
        // Показываем актуальный X ползунка, его выигрыш и шанс. Нажатие
        // применяет X как цель (applyCustomMult), а не «прыгает» само.
        {
            const customMult = Math.max(UPGRADE_MULT_MIN,
                Math.min(UPGRADE_MULT_MAX, upgradeMultFromSlider(
                    Number((document.getElementById('up-custom-range') || {}).value) || 0)));
            const customChance = upgradeDisplayChance(upgradeMultChance(customMult));
            const customTo = fromValue * customMult;
            const customActive = upgradeTarget.kind === 'mult'
                && Math.abs(Number(upgradeTarget.value) - customMult) < 0.005;
            tiles.push({
                key: 'custom',
                active: customActive,
                disabled: !upgradeChanceIsValid(customChance),
                html: `<div class="up-to-tile-icon">✏️</div>
                       <div class="up-to-tile-name">x${customMult.toFixed(2)}</div>
                       <div class="up-to-tile-value">${coinAmount(customTo)}</div>
                       <div class="up-to-tile-chance">${customChance.toFixed(2)}%</div>`
            });
        }
        if (upgradeTarget.kind === 'chance') {
            const toValue = upgradeTargetValue(fromValue);
            const ratio = fromValue > 0 && toValue > 0 ? (fromValue / toValue) * 100 : 0;
            const shownChance = upgradeDisplayChance(ratio);
            const chance = shownChance;
            tiles.push({
                key: `chance:${upgradeTarget.value}`,
                active: true,
                html: `<div class="up-to-tile-icon">🎯</div>
                       <div class="up-to-tile-name">${Number(upgradeTarget.value).toFixed(0)}%</div>
                       <div class="up-to-tile-value">${coinAmount(toValue)}</div>
                       <div class="up-to-tile-chance">${shownChance.toFixed(2)}%</div>`
            });
        }
    } else {
        UPGRADE_ITEM_CATALOG.forEach(item => {
            const itemId = item.id;
            const toValue = upgradePrice(itemId);
            const shownChance = upgradeDisplayChance(fromValue > 0 && toValue > 0
                ? (fromValue / toValue) * 100 : 0);
            const chance = shownChance;
            const active = upgradeTarget.kind === 'item' && upgradeTarget.value === itemId;
            const disabled = !upgradeChanceIsValid(chance);
            tiles.push({
                key: `item:${itemId}`,
                active,
                disabled,
                html: `<div class="up-to-tile-icon">${upgradeItemEmoji(itemId)}</div>
                       <div class="up-to-tile-name">${item.short}</div>
                       <div class="up-to-tile-value">${coinAmount(toValue)}</div>
                       <div class="up-to-tile-chance">${disabled ? '—' : shownChance.toFixed(2) + '%'}</div>`
            });
        });
    }

    grid.innerHTML = tiles.map(tile => `
        <button class="up-to-tile${tile.active ? ' active' : ''}${tile.disabled ? ' disabled' : ''}"
                data-target="${tile.key}" ${tile.disabled ? 'disabled' : ''}>${tile.html}</button>
    `).join('');

    grid.querySelectorAll('.up-to-tile').forEach(button => {
        if (button.disabled) return;
        button.addEventListener('click', () => {
            const raw = button.dataset.target || '';
            const [kind, value] = raw.split(':');
            // Задача 52: кастомный X живёт прямо в сетке цели. Раньше он был
            // ползунком, который сам менял цель, и выбранная плитка «прыгала».
            if (kind === 'custom') { applyCustomMult(); return; }
            if (kind === 'item') selectUpgradeTarget('item', value);
            else selectUpgradeTarget(kind, Number(value));
        });
    });
}

// ================= ЦЕЛЬ ПРИ СТАВКЕ КАПСУЛОЙ (задача 65) =================
// На скрине заказчика: слева капсула, справа на круге «100.00%», под кольцом
// «выбери цель подороже» — и выбрать её НЕЛЬЗЯ. Причина: при источнике-капсуле
// цель жёстко ставилась в {kind:'item', value:'capsule'}, то есть «обычная
// капсула → обычная капсула»: шанс всегда 100%, а сетка целей была спрятана
// (её убрали в задаче 56 вместе с множителями). Теперь сетка целей-предметов
// показывается при капсуле: цель можно выбрать, и «подороже» действительно есть.
let upgradeItemGridVisible = false;
// Задача 65: апгрейд капсулы в КОИНЫ. Нужен, когда ставить нечего дороже:
// например галактическая капсула (10 000) — самая дорогая в игре, «цель
// подороже» для неё не существует, и предметный апгрейд всегда упирался бы в
// 100%. Тогда цель — коины, шанс считается как обычно 90 / X.
let upgradeCapsuleCoinTarget = false;

function renderUpgradeItemTargets() {
    const grid = document.getElementById('up-to-grid');
    if (!grid) return;
    if (!upgradeItemGridVisible) {
        grid.hidden = true; grid.style.display = 'none';
        return;
    }
    grid.hidden = false; grid.style.display = '';
    const fromValue = upgradeSourceValue();
    // Плитки: предметы + «Коины» (апгрейд капсулы в коины по множителю).
    const coinActive = upgradeCapsuleCoinTarget && upgradeTarget.kind === 'mult';
    const coinTile = `<button type="button" class="up-to-tile${coinActive ? ' active' : ''}"`
        + ` data-item="__coin">`
        + `<div class="up-to-tile-icon"><img class="coin-img" src="coin.png" alt="монета"></div>`
        + `<div class="up-to-tile-name">Коины</div>`
        + `<div class="up-to-tile-value">x${Number(upgradeTarget.kind === 'mult' ? upgradeTarget.value : UPGRADE_DEFAULT_MULT).toFixed(2)}</div>`
        + `<div class="up-to-tile-chance">${upgradeDisplayChance(upgradeMultChance(upgradeTarget.kind === 'mult' ? upgradeTarget.value : UPGRADE_DEFAULT_MULT)).toFixed(2)}%</div>`
        + `</button>`;
    grid.innerHTML = coinTile + UPGRADE_ITEM_CATALOG.map(item => {
        const toValue = upgradePrice(item.id);
        const shownChance = upgradeDisplayChance(fromValue > 0 && toValue > 0
            ? (fromValue / toValue) * 100 : 0);
        const active = upgradeTarget.kind === 'item' && upgradeTarget.value === item.id;
        // Цель дешевле ставки = шанс выше 95%: сервер такой апгрейд отклонит,
        // поэтому плитка неактивна и честно говорит «дешевле», а не «дешевле
        // или равно».
        const tooCheap = shownChance > UPGRADE_MAX_CHANCE;
        const disabled = !upgradeChanceIsValid(shownChance) && !tooCheap;
        return `<button type="button" class="up-to-tile${active ? ' active' : ''}`
            + `${tooCheap ? ' hard-disabled' : ''}${disabled ? ' disabled' : ''}"`
            + ` data-item="${item.id}" ${tooCheap ? 'disabled' : ''}>`
            + `<div class="up-to-tile-icon">${upgradeItemEmoji(item.id)}</div>`
            + `<div class="up-to-tile-name">${item.short}</div>`
            + `<div class="up-to-tile-value">${coinAmount(toValue)}</div>`
            + `<div class="up-to-tile-chance">${tooCheap ? 'дешевле' : shownChance.toFixed(2) + '%'}</div>`
            + `</button>`;
    }).join('');
    grid.querySelectorAll('.up-to-tile').forEach(button => {
        button.addEventListener('click', () => {
            if (button.disabled) return;
            if (button.dataset.item === '__coin') {
                // Капсула → коины: цель снова множитель, шанс 90 / X.
                upgradeCapsuleCoinTarget = true;
                upgradeTarget = { kind: 'mult', value: UPGRADE_DEFAULT_MULT };
                clearUpgradeResult();
                playSound('click');
                renderUpgrade();
                return;
            }
            upgradeCapsuleCoinTarget = false;
            selectUpgradeTarget('item', button.dataset.item);
        });
    });
}

// Подобрать ближайшую ДОРОЖЕ цель, чтобы шанс стал допустимым. Нужно для кнопки
// «выбрать цель подороже» и для автоматического перевода формы в рабочее
// состояние: игрок видит 100% только пока сам не выбрал цель, а не как тупик.
function upgradePickDearerTarget() {
    const fromValue = upgradeSourceValue();
    const options = UPGRADE_ITEM_CATALOG
        .map(item => ({ id: item.id, price: upgradePrice(item.id) }))
        .filter(item => item.price > fromValue)          // строго дороже ставки
        .sort((a, b) => a.price - b.price);
    return options.length ? options[0].id : null;
}

// Быстрые пресеты множителя (задача 56). Раньше это были шесть плиток с ценой
// и шансом — они занимали пол-экрана и дублировали ползунок. Теперь компактные
// чипы: тап выбирает ровно тот же X, что и ползунок.
function renderUpgradePresets() {
    const box = document.getElementById('up-presets');
    if (!box) return;
    if (upgradeFrom !== 'coin') {
        box.innerHTML = '';
        box.hidden = true;
        return;
    }
    box.hidden = false;
    box.innerHTML = UPGRADE_MULTS.map(mult => {
        const active = upgradeTarget.kind === 'mult'
            && Math.abs(Number(upgradeTarget.value) - mult) < 0.005;
        const chance = upgradeDisplayChance(upgradeMultChance(mult));
        return `<button type="button" class="up-preset${active ? ' active' : ''}" `
            + `data-mult="${mult}" onclick="selectUpgradePreset(${mult})" `
            + `title="x${mult} · ${chance.toFixed(2)}%">`
            + `x${mult}<span>${chance.toFixed(0)}%</span></button>`;
    }).join('');
}

function selectUpgradePreset(mult) {
    if (upgradeBusy) return;
    const value = Math.max(UPGRADE_MULT_MIN, Math.min(UPGRADE_MULT_MAX, Number(mult) || UPGRADE_MULT_MIN));
    upgradeToTab = 'coin';
    upgradeTarget = { kind: 'mult', value };
    const slider = document.getElementById('up-custom-range');
    if (slider) slider.value = String(upgradeSliderFromMult(value));
    clearUpgradeResult();
    playSound('click');
    renderUpgrade();
}

function selectUpgradeFrom(type, element) {
    if (upgradeBusy) return;
    const known = type === 'coin' || UPGRADE_ITEM_CATALOG.some(item => item.id === type);
    if (!known) return;
    if (type !== 'coin' && countItem(type) === 0) {
        tg.showAlert(`У тебя нет капсул «${upgradeItemName(type)}»`);
        return;
    }
    upgradeFrom = type;
    upgradeFromAmount = type === 'coin'
        ? Math.min(UPGRADE_MAX_COINS, Math.max(UPGRADE_MIN_COINS, upgradeFromAmount))
        : Math.min(countItem(type), UPGRADE_MAX_CAPSULES, Math.max(1, upgradeFromAmount));
    document.querySelectorAll('.up-from-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.from === type));
    if (element) element.classList.add('active');
    clearUpgradeResult();
    playSound('click');
    renderUpgrade();
}

function selectUpgradeToTab(tab, element) {
    if (upgradeBusy) return;
    upgradeToTab = tab === 'capsule' ? 'capsule' : 'coin';
    document.querySelectorAll('.up-to-tab').forEach(btn => btn.classList.remove('active'));
    if (element) element.classList.add('active');
    if (upgradeToTab === 'coin' && upgradeTarget.kind === 'item') upgradeTarget = { kind: 'mult', value: UPGRADE_DEFAULT_MULT };
    if (upgradeToTab === 'capsule' && upgradeTarget.kind !== 'item') upgradeTarget = { kind: 'item', value: 'capsule' };
    clearUpgradeResult();
    playSound('click');
    renderUpgrade();
}

function selectUpgradeTarget(kind, value) {
    if (upgradeBusy) return;
    upgradeToTab = kind === 'item' ? 'capsule' : 'coin';
    document.querySelectorAll('.up-to-tab').forEach(btn => btn.classList.toggle('active', btn.id === 'up-to-tab-' + upgradeToTab));
    upgradeTarget = { kind, value };
    clearUpgradeResult();
    playSound('click');
    renderUpgrade();
}

function setUpgradeAmount(value) {
    if (upgradeBusy) return;
    const limits = upgradeAmountLimits();
    // MAX ставит потолок лимита (для коинов это ровно 5 000), а не весь баланс:
    // у админа на счету бывает 980 200, одной ставкой их ставить нельзя.
    let next = value === 'max' ? Math.min(UPGRADE_MAX_COINS, limits.max) : Number(value);
    if (!Number.isFinite(next)) return;
    next = Math.min(limits.max, Math.max(limits.min, Math.round(next)));
    upgradeFromAmount = next;
    clearUpgradeResult();
    renderUpgrade();
}

function syncUpgradeAmountFromInput() {
    const input = document.getElementById('upgrade-amount');
    if (!input) return;
    const limits = upgradeAmountLimits();
    let value = Math.round(Number(input.value));
    if (!Number.isFinite(value)) value = limits.min;
    value = Math.min(limits.max, Math.max(limits.min, value));
    upgradeFromAmount = value;
    clearUpgradeResult();
    renderUpgrade();
}

// Профиль может ещё грузиться — тогда isAdmin ещё false, и админ зря увидел бы «Скоро...».
async function ensureUserLoaded() {
    if (userDataLoaded) return true;
    try { await loadUserData(); } catch (e) { console.error('Не удалось загрузить профиль', e); }
    return userDataLoaded;
}

// Доступ к Апгрейду (задача 62/65): открыт всем, кто может играть. Раньше
// решал список ADMIN_UI_IDS — обычный игрок видел серую карточку «Скоро...».
function upgradeAccessGranted() {
    // Задача 67: апгрейд тоже открыт всем — доступ решает только сервер,
    // который теперь никого не блокирует.
    return true;
}

// Текст отказа: не «Скоро...» (оно выглядело как «апгрейд закрыт навсегда»),
// а причина от сервера — техработы или бан.
function accessBlockedText() {
    return maintenanceMessage || 'Игра сейчас на техработах — загляни чуть позже';
}

function updateUpgradeAccess() {
    const allowed = upgradeAccessGranted();
    const card = document.getElementById('game-card-upgrade');
    if (card) {
        card.classList.toggle('locked', !allowed);
        card.setAttribute('aria-disabled', allowed ? 'false' : 'true');
    }
    const lock = document.getElementById('upgrade-lock-icon');
    if (lock) lock.style.display = allowed ? 'none' : 'inline';
    const sub = document.getElementById('upgrade-card-sub');
    if (sub) sub.innerText = allowed ? 'Улучшай капсулы' : 'Техработы';
}

function updateUpgradeStatsUI() {
    // innerHTML: в текстах встречается картинка монеты.
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.innerHTML = value; };
    setText('upg-stat-total', upgradeStats.total);
    setText('upg-stat-wins', upgradeStats.wins);
    setText('upg-stat-best', upgradeStats.best > 0 ? formatCoins(upgradeStats.best) : '—');
}

// Задача 54: сверка баланса по журналу апгрейдов. Профиль (/api/user) мог
// прийти до того, как сервер записал последний раунд; логи же пишутся вместе с
// балансом, поэтому именно по ним видно, что ставка уже списана/выплачена.
async function syncUpgradeBalanceFromLog() {
    if (!tg.initData) return false;
    let data = null;
    try {
        const res = await fetch(`${API_URL}/api/upgrade_pending`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, limit: 10 })
        });
        if (!res.ok) return false;
        data = await res.json().catch(() => ({}));
    } catch (e) {
        return false;
    }
    if (!data || data.available === false) {
        // Таблицы логов нет — но баланс сервер всё равно прислал.
        const coins = Number(data && data.coins);
        if (Number.isFinite(coins)) { balance = coins; balanceKnown = true; updateUI(); return true; }
        return false;
    }
    const rounds = Array.isArray(data.rounds) ? data.rounds : [];
    const coins = Number(data.coins);
    const fresh = rounds.length > 0 && rounds[0];
    const seenFresh = fresh && upgradeShownLogIds.has(Number(fresh.id));

    if (upgradeBusy) {
        // Раунд ещё идёт: серверное число — это либо «до ставки», либо уже
        // новый баланс. Ничего не меняем, финальный ответ всё поправит.
        return false;
    }
    // Задача 60: блокировка снята (раунд закончился/просрочен) — серверное число
    // единственная правда, даже если оно больше текущего (выигрыш). Раньше здесь
    // стояло «coins <= balance», поэтому после обнуления баланс не возвращался.
    const logLockLive = upgradeBalanceLockLive();
    if (!logLockLive && (upgradeRoundActive || Number.isFinite(upgradeBalanceFloor))) {
        clearUpgradeBalanceLock();
    }
    const serverBalance = Number.isFinite(coins);
    if (serverBalance && (!logLockLive || coins <= balance)) {
        balance = coins;
        balanceKnown = true;
        updateUI();
    } else if (!serverBalance) {
        // Задача 60: сервер вообще не отдал баланс (users недоступен) — вместо
        // того чтобы молчать, спрашиваем профиль. Раньше число из журнала
        // подставлялось как есть, а с новым контрактом coins=null молчание
        // оставило бы игрока со старым значением на экране.
        loadUserData().catch(() => {});
    }
    // Раунд сыгран, но игрок его не видел именно СЕЙЧАС (закрыл аппку на
    // прокруте) — показываем результат. Старые раунды не всплывают: их
    // показом занимается checkPendingUpgradeResult().
    const recentMs = 10 * 60 * 1000;
    const age = fresh && fresh.created_at ? Date.now() - Date.parse(fresh.created_at) : 0;
    const justFinished = fresh && !seenFresh && fresh.won !== undefined
        && Number.isFinite(age) && age >= 0 && age < recentMs;
    if (justFinished) {
        // coins из /api/upgrade_pending — живой баланс из users (задача 60).
        showMissedUpgradeResult(fresh, coins);
    }
    return false;
}

async function openUpgrade() {
    // Задача 67: апгрейд открыт всем без исключений — админ-проверок больше нет.
    if (!upgradeAccessGranted()) {
        return tg.showAlert(accessBlockedText());
    }
    const loaded = await ensureUserLoaded();
    if (!loaded) return tg.showAlert('Не удалось загрузить профиль. Проверьте соединение.');
    if (!upgradeAccessGranted()) {
        updateUpgradeAccess();
        return tg.showAlert(accessBlockedText());
    }
    // Если раунд «пропал» (ни анимации, ни ожидания ответа), не блокируем вход:
    // раньше кнопка навсегда оставалась в «КРУТИМ...» и перезаход не помогал.
    if (upgradeBusy && upgradeArrowRaf === null && !upgradeFetchPending) releaseUpgradeBusy();
    // Задача 60: вход в игру — точка, где «залипший» раунд видно лучше всего.
    // Если сервер сейчас ничего не считает, блокировка баланса не нужна: без
    // этого она жила до перезагрузки страницы и резала баланс в ноль.
    if (!upgradeBalanceLockLive()) clearUpgradeBalanceLock();
    // Не сбрасываем незавершённый раунд: раньше вход в игру во время прокрутки
    // «съедал» результат (ставка списана, приз не показан).
    if (!upgradeBusy) {
        upgradeSpinToken++;      // отменяем возможную недоигранную анимацию
        resetUpgradeArrow();
        clearUpgradeResult();
    }
    openGame('upgrade');
    renderUpgrade();
    await checkPendingUpgradeResult();   // показать то, что игрок не увидел в прошлый раз
    // Задача 54: даже если лог недоступен, баланс сверяем с сервером при входе —
    // иначе после выхода в меню игрок видел сумму «до ставки».
    await loadUserData().catch(() => {});
    await syncUpgradeBalanceFromLog().catch(() => {});
}

// ================= БЕЛАЯ СТРЕЛКА =================
function setUpgradeArrowAngle(deg, options) {
    upgradeArrowAngle = deg;
    const arrow = document.getElementById('up-arrow');
    if (!arrow) return;
    // По умолчанию угол ставится мгновенно (фаза спина, сброс). Плавный финиш
    // включает transition сам и вызывает этот хелпер с keepTransition.
    if (!(options && options.keepTransition)) arrow.style.transition = 'none';
    arrow.style.transform = `rotate(${deg}deg)`;
}

function stopUpgradeArrow() {
    if (upgradeArrowRaf !== null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(upgradeArrowRaf);
    upgradeArrowRaf = null;
}

function resetUpgradeArrow() {
    stopUpgradeArrow();
    setUpgradeArrowAngle(0);
}

// Фаза 1: пока ждём ответ сервера — стрелка просто ровно крутится.
function startUpgradeArrowSpin(token) {
    stopUpgradeArrow();
    const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
    let last = nowMs();
    const step = () => {
        if (token !== upgradeSpinToken) return;
        const now = nowMs();
        const dt = Math.max(0, Math.min(now - last, 120));
        last = now;
        setUpgradeArrowAngle(upgradeArrowAngle + UPGRADE_ARROW_SPEED * dt / 1000);
        upgradeArrowRaf = typeof requestAnimationFrame === 'function'
            ? requestAnimationFrame(step)
            : setTimeout(() => step(), 16);
    };
    upgradeArrowRaf = typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame(step)
        : setTimeout(() => step(), 16);
}

// Куда встанет стрелка. Если сервер прислал бросок (roll), стрелка показывает
// ИМЕННО его: угол = upgradeAngleForPercent(roll) — та же формула, что красит
// зелёную зону круга и белую риску границы.
//
// Единственное исключение — сама граница. Стрелка толще, чем 0.1 п.п., поэтому
// при броске «впритык» она визуально наезжала на линию, и победа выглядела как
// «стрелка в красной зоне». Теперь у границы держится видимый зазор:
//   победа  → стрелка гарантированно ВНУТРИ зелёного (arc − зазор);
//   проигрыш → стрелка гарантированно ВНУТРИ красного (arc + зазор).
// Дальше зазора от границы стрелка стоит ровно на броске, без искажений.
const UPGRADE_BOUNDARY_NUDGE_PCT = 0.1;      // логический минимум сдвига, п.п.
const UPGRADE_ARROW_CLEARANCE_PCT = 0.6;     // видимый зазор до линии, п.п.

function upgradeArrowClearanceDeg(arc) {
    const nudge = upgradeAngleForPercent(UPGRADE_BOUNDARY_NUDGE_PCT);
    const wanted = upgradeAngleForPercent(UPGRADE_ARROW_CLEARANCE_PCT);
    // У очень маленького шанса зелёная зона уже зазора — берём половину дуги,
    // чтобы стрелка всё равно осталась внутри зелёного.
    const limit = Math.max(0.6, arc * 0.5);
    return Math.max(nudge, Math.min(wanted, limit));
}

// Зазор для ПРОИГРЫША считается от длины красной зоны (360° − дуга):
// при шансе 99.99% красная зона — это 0.036°, и зазор в 2.16° вынес бы стрелку
// за 360° (в ноль, то есть обратно в зелёный). Поэтому зазор пропорциональный.
function upgradeArrowRedClearanceDeg(arc) {
    const room = Math.max(0, 360 - arc);
    const nudge = upgradeAngleForPercent(UPGRADE_BOUNDARY_NUDGE_PCT);
    const wanted = upgradeAngleForPercent(UPGRADE_ARROW_CLEARANCE_PCT);
    return Math.max(Math.min(nudge, room * 0.5), Math.min(wanted, room * 0.6));
}

function upgradeArrowTargetFor(won, chance, rollValue) {
    const chanceNumber = Math.max(0, Math.min(100, Number(chance) || 0));
    const arc = Math.max(0.5, Math.min(360, upgradeAngleForPercent(chanceNumber)));
    const clearance = upgradeArrowClearanceDeg(arc);
    const redClearance = upgradeArrowRedClearanceDeg(arc);
    const rollNumber = Number(rollValue);
    if (Number.isFinite(rollNumber)) {
        // Угол по броску — той же формулой, что и заливка круга.
        let desired = Math.max(0, Math.min(359.99, upgradeAngleForPercent(rollNumber)));
        // Жёсткая гарантия: стрелка ВСЕГДА в зоне, которую назвал вердикт.
        // Односторонний зажим (а не «сдвиг только у самой границы») закрывает и
        // случай противоречивых чисел от сервера: победа с броском в красной зоне
        // всё равно будет показана внутри зелёного.
        if (won) {
            if (desired > arc - clearance) desired = Math.max(0, Math.min(arc - clearance, 359.99));
        } else {
            // 100% — это всегда победа (roll <= 100), защищаемся от кривого ответа.
            const minimum = Math.min(359.99, arc + redClearance);
            if (desired < minimum) desired = minimum;
        }
        return desired;
    }
    // Броска нет (старый ответ сервера) — случайная позиция в нужной зоне.
    if (won) {
        const margin = Math.min(arc * 0.18, Math.max(arc * 0.1, 2));
        return margin + Math.random() * Math.max(arc - margin * 2, arc * 0.25);
    }
    const free = Math.max(360 - arc, 4);
    return arc + free * 0.18 + Math.random() * free * 0.64;
}

// Фаза 2 — «как в рулетке»: несколько полных оборотов и плавное затухание
// ровно на выпавшем числе. Раньше торможение считалось вручную кусками, и на
// стыке фаз стрелка дёргалась. Теперь один CSS-transition с кривой
// cubic-bezier(0.1, 0.7, 0.1, 1) на 5 секунд — без рывков и без «доводки».
function landUpgradeArrow(won, chance, duration, token, onDone, rollValue) {
    stopUpgradeArrow();
    const arrow = document.getElementById('up-arrow');
    const desired = ((upgradeArrowTargetFor(won, chance, rollValue) % 360) + 360) % 360;
    const current = ((upgradeArrowAngle % 360) + 360) % 360;
    const delta = ((desired - current) % 360 + 360) % 360;
    // Обороты: не меньше трёх — иначе при совпадении углов стрелка «не крутится».
    const turns = Math.max(3, Math.ceil(UPGRADE_LAND_MS / 900));
    const target = upgradeArrowAngle + turns * 360 + delta;
    const total = Math.max(1500, Math.min(UPGRADE_LAND_MS, Number(duration) || UPGRADE_LAND_MS));
    let finished = false;
    let fallbackTimer = null;      // объявлен до finish: иначе TDZ при раннем выходе

    const finish = () => {
        if (finished) return;
        finished = true;
        if (arrow && arrow.removeEventListener) arrow.removeEventListener('transitionend', onTransitionEnd);
        if (fallbackTimer) clearTimeout(fallbackTimer);
        if (token !== upgradeSpinToken) return;
        // Фиксируем конечный угол: он уже кратен нужному (target ≡ desired mod 360).
        setUpgradeArrowAngle(target);
        if (typeof onDone === 'function') onDone();
    };
    const onTransitionEnd = (event) => {
        if (!event || event.target === arrow) finish();
    };

    if (!arrow || typeof arrow.style.transition !== 'string') {
        upgradeArrowAngle = target;
        finish();
        return;
    }

    // 1) Стартовый угол без анимации.
    setUpgradeArrowAngle(upgradeArrowAngle);
    // 2) Форсируем reflow: без него браузер склеит два transform и transition
    //    не запустится (стрелка телепортируется).
    void arrow.offsetWidth;
    // 3) Плавный финиш одной кривой.
    arrow.style.transition = `transform ${total}ms ${UPGRADE_LAND_EASING}`;
    upgradeArrowAngle = target;
    arrow.style.transform = `rotate(${target}deg)`;
    if (arrow.addEventListener) arrow.addEventListener('transitionend', onTransitionEnd);
    // Страховка: вкладка была свёрнута и transition не доехал.
    fallbackTimer = setTimeout(finish, total + 300);
}

// ================= СПИСАНИЕ СТАВКИ СРАЗУ =================
// Задача 29: деньги должны уходить в момент ставки, а не после анимации.
// Сервер списывает ставку ДО броска (upgrade_action), но клиент показывал
// старый баланс все 3.4 секунды прокрутки — со стороны это выглядело так,
// будто ставка не списалась. Показываем списание сразу, а итоговый баланс
// всё равно приходит с сервера в applyUpgradeServerState.
let upgradeStakePreview = null;

// Задача 54: «потолок» баланса на время раунда. Фоновый refresh профиля
// (/api/user) приходит с балансом «до ставки» — без этого ограничения игрок на
// миг видит старую сумму и решает, что списание не произошло.
let upgradeBalanceFloor = null;
let upgradeRoundActive = false;
// Задача 54: ставка, которая уже ушла на сервер. Если профиль пришёл с
// балансом «до списания» (или пришёл позже ответа), эту сумму вычитаем сами,
// иначе игрок видит старый баланс и думает, что ставка не списалась.
let upgradePendingStake = 0;
// Задача 60: когда блокировка поставлена. Ограничение живёт только на время
// реального раунда — иначе «залипший» раунд резал баланс при каждом обновлении
// профиля (вплоть до 0 при ставке «всё в банк»).
let upgradeBalanceLockedAt = 0;
const UPGRADE_BALANCE_LOCK_TTL = 60000;

function lockUpgradeBalance(baseBalance) {
    if (Number.isFinite(Number(baseBalance))) upgradeBalanceFloor = Number(baseBalance);
    upgradeBalanceLockedAt = Date.now();
}

// Блокировка баланса имеет смысл ТОЛЬКО пока раунд действительно идёт с
// сервером. Раньше флаги снимались лишь в applyUpgradeServerState и
// restoreUpgradeStakePreview: если ответ терялся, сторож (15 с) оживлял кнопку,
// а upgradeRoundActive/upgradeBalanceFloor оставались взведёнными навсегда.
// Дальше каждое обновление профиля считало limit = floor - stake и обрезало
// баланс — на скрине игрока оставался 0 при ставке 100. Теперь просроченная
// блокировка снимается сама.
function upgradeBalanceLockLive() {
    if (!Number.isFinite(upgradeBalanceFloor)) return false;
    // Раунд уже не идёт (ответ пришёл, сторож снял busy, игрок вышел) —
    // ограничивать нечего.
    if (!(upgradeBusy || upgradeFetchPending)) return false;
    if (Date.now() - upgradeBalanceLockedAt > UPGRADE_BALANCE_LOCK_TTL) return false;
    return true;
}

function clearUpgradeBalanceLock() {
    upgradeRoundActive = false;
    upgradeBalanceFloor = null;
    upgradePendingStake = 0;
    upgradeBalanceLockedAt = 0;
}

function applyUpgradeStakePreview() {
    const amount = Math.max(0, Math.round(Number(upgradeFromAmount) || 0));
    if (!amount || upgradeStakePreview) return null;
    // Задача 60: upgradePendingStake ставим только когда превью реально
    // применилось. Раньше он выставлялся до проверок, и при неудаче в памяти
    // оставалась «ставка», которой на балансе уже нет.
    const preview = { source: upgradeFrom, amount, inventory: null };
    if (upgradeFrom === 'coin') {
        // Без известного баланса «минусовать» нечего — ждём сервер.
        if (!balanceKnown || balance < amount) return null;
        preview.balanceBefore = balance;
        balance -= amount;
    } else {
        let left = amount;
        const next = [];
        inventory.forEach(item => {
            if (item === upgradeFrom && left > 0) { left -= 1; return; }
            next.push(item);
        });
        if (left > 0) return null;             // столько капсул нет — сервер откажет
        preview.inventoryBefore = inventory;
        inventory = next;
    }
    upgradePendingStake = amount;      // сумма, которую сервер списал (или спишет)
    upgradeStakePreview = preview;
    updateUI();
    return preview;
}

function restoreUpgradeStakePreview() {
    const preview = upgradeStakePreview;
    upgradeStakePreview = null;
    // Раунд не состоялся — баланс снова обычный (задача 60: снимаем всю
    // блокировку разом, чтобы она не пережила раунд).
    clearUpgradeBalanceLock();
    if (!preview) return;
    if (preview.source === 'coin' && Number.isFinite(preview.balanceBefore)) {
        balance = preview.balanceBefore;
        balanceKnown = true;
    } else if (preview.inventoryBefore) {
        inventory = preview.inventoryBefore;
    }
    updateUI();
}

async function startUpgrade() {
    if (upgradeBusy) return;
    if (!upgradeAccessGranted()) { updateUpgradeAccess(); return tg.showAlert(accessBlockedText()); }

    // Блокировка ставится до любых await: иначе двойной клик во время загрузки
    // профиля отправил бы две ставки.
    upgradeBusy = true;
    upgradeAwaitingRestart = false;
    upgradeFetchPending = true;
    armUpgradeBusyWatchdog();

    try {
        const loaded = await ensureUserLoaded();
        if (!loaded) {
            upgradeFetchPending = false;
            releaseUpgradeBusy();
            return tg.showAlert('Не удалось загрузить баланс. Проверьте соединение.');
        }
        // Задача 65: та же проверка, что и у карточки — апгрейд открыт всем.
        if (!upgradeAccessGranted()) {
            upgradeFetchPending = false;
            releaseUpgradeBusy();
            return tg.showAlert('Апгрейд сейчас недоступен — обновите приложение');
        }
        if (!upgradeCanStart()) {
            upgradeFetchPending = false;
            releaseUpgradeBusy();
            // Задача 54: шанс вне диапазона больше не блокирует кнопку —
            // показываем подсказку, но игру не отменяем молча.
            if (!upgradeChanceIsValid(upgradeChance())) {
                tg.showAlert(upgradeChance() > UPGRADE_MAX_CHANCE
                    ? 'Шанс выше 95% — цель слишком дешёвая, выбери подороже'
                    : 'Шанс ниже 0.5% — цель слишком дорогая, выбери подешевле');
            }
            if (upgradeFrom === 'coin' && balance < upgradeFromAmount) return tg.showAlert('Недостаточно коинов!');
            if (upgradeFrom !== 'coin' && countItem(upgradeFrom) < upgradeFromAmount) return tg.showAlert('Недостаточно капсул!');
            return tg.showAlert('Такой апгрейд сейчас невозможен');
        }
    } catch (e) {
        upgradeFetchPending = false;
        releaseUpgradeBusy();
        console.error('Ошибка подготовки апгрейда', e);
        return tg.showAlert('Не удалось подготовить апгрейд');
    }

    // id раунда запоминаем ДО отправки: если ответ потеряется, по нему можно
    // догнать уже сыгранный раунд (задача 70) и не потерять ставку.
    const roundId = requestId();
    lastUpgradeRequestId = roundId;
    const payload = {
        initData: tg.initData,
        item_from: upgradeFrom,
        amount_from: Math.round(upgradeFromAmount),
        item_to: upgradeTarget.kind === 'item' ? upgradeTarget.value : 'coin',
        // Ключ идемпотентности: повторная отправка не спишет ставку второй раз.
        request_id: roundId
    };
    if (upgradeTarget.kind === 'mult') payload.target_multiplier = Number(upgradeTarget.value);
    if (upgradeTarget.kind === 'chance') payload.target_chance = Number(upgradeTarget.value);

    // Ставка уходит с баланса сразу — до анимации и до ответа сервера.
    // База фиксируется ДО списания: столько уже точно не должно вернуться.
    // Задача 60: флаг раунда ставим только вместе с живой блокировкой — иначе
    // «потолок» баланса жил дольше самого раунда (см. upgradeBalanceLockLive).
    lockUpgradeBalance(balance);
    upgradeRoundActive = true;
    upgradeBalanceLockedAt = Date.now();
    applyUpgradeStakePreview();

    const chance = upgradeChance();
    // Пока стрелка крутится, дуга может показывать текущую форму — это не важно.
    // Фиксируем её, как только придёт ответ сервера (см. ниже): именно серверный
    // шанс объясняет результат, и до сброса формы он не должен меняться.
    releaseUpgradeChance();
    upgradeStartedAt = Date.now();
    const token = ++upgradeSpinToken;   // всё, что прилетит от прошлых прогонов, игнорируем

    // Новый раунд: круг из состояния «победа/проигрыш» снова в исходное,
    // прошлый итог под кругом убираем.
    const circle = document.getElementById('up-circle');
    if (circle) { circle.classList.remove('win', 'lose'); circle.classList.add('spinning'); }
    clearUpgradeNet();
    const btn = document.getElementById('upgrade-btn');
    if (btn) { btn.disabled = true; btn.classList.remove('win', 'lose'); btn.innerText = 'КРУТИМ...'; }
    setUpgradeResult('Крутим...', 'spinning');
    startUpgradeArrowSpin(token);
    playSound('click');

    let data = null;
    let errorText = '';
    let unauthorized = false;
    let timedOut = false;
    const controller = (typeof AbortController === 'function') ? new AbortController() : null;
    // Задача 70: 45 с вместо 8 с. Холодный Render просыпается дольше, и раньше
    // запрос обрывался на середине: игрок читал «Сервер не ответил», а ставка
    // к этому моменту уже была списана и разыграна.
    const abortTimer = controller ? setTimeout(() => { timedOut = true; controller.abort(); }, UPGRADE_FETCH_TIMEOUT) : null;
    try {
        const options = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        };
        if (controller) options.signal = controller.signal;
        const res = await fetch(`${API_URL}/api/upgrade_action`, options);
        data = await res.json().catch(() => ({}));
        if (!res.ok) {
            errorText = data.detail || 'Не удалось запустить апгрейд';
            unauthorized = res.status === 403;
            data = null;
        }
    } catch (e) {
        console.error('Ошибка апгрейда', e);
        errorText = timedOut ? 'Сервер не ответил. Проверьте соединение' : 'Ошибка соединения';
    } finally {
        if (abortTimer) clearTimeout(abortTimer);
        upgradeFetchPending = false;
    }

    if (token !== upgradeSpinToken) {
        // Раунд отменён (например, открыли экран заново). Результат всё равно
        // пришёл с сервера: баланс и инвентарь надо применять, иначе игрок увидит
        // «ставку списали, приза нет» — и обязательно снимаем блокировку, иначе
        // кнопка навсегда оставалась в «КРУТИМ...» (то самое «залипание»).
        releaseUpgradeBusy();
        if (data) {
            applyUpgradeServerState(data);
            try { updateUI(); } catch (e) {}
            await loadUserData().catch(() => {});
            checkPendingUpgradeResult();
        }
        return;
    }

    // Общая длительность анимации — 3.4 секунды: остаток времени уходит на торможение стрелки.
    const elapsed = Date.now() - upgradeStartedAt;
    const remaining = Math.max(UPGRADE_ARROW_MIN_LAND, UPGRADE_DURATION - elapsed);

    if (errorText || !data) {
        stopUpgradeArrow();
        // Задача 70: прежде чем говорить «сервер не ответил», проверяем, не
        // сыгран ли раунд на самом деле. Раньше ставка уже была списана и
        // разыграна, а игрок видел ошибку — деньги «сгорали».
        const played = await fetchPlayedUpgrade(lastUpgradeRequestId);
        if (played) {
            console.info('Апгрейд: ответ потерялся, раунд забран повторно', played);
            applyUpgradeServerState(played);
            if (btn) { btn.disabled = false; btn.classList.remove('win', 'lose'); }
            finishUpgradeAnimation(played, upgradeEffectiveChance(played, chance) || chance);
            try { updateUI(); } catch (e) {}
            return;
        }
        // Ставка не сыграла — возвращаем показанный баланс/капсулы на место.
        restoreUpgradeStakePreview();
        if (circle) circle.classList.remove('spinning', 'win', 'lose');
        releaseUpgradeBusy();
        setUpgradeResult(errorText || 'Не удалось запустить апгрейд', 'error');
        if (btn) { btn.disabled = false; btn.innerText = 'АПГРЕЙД'; }
        // 403 теперь значит только «техработы/бан» — права админа не трогаем,
        // иначе после одного такого ответа апгрейд запирался до перезагрузки.
        if (unauthorized) updateUpgradeAccess();
        tg.showAlert(errorText || 'Не удалось запустить апгрейд');
        renderUpgrade();
        // Ставка могла уйти на сервер, даже если ответ потерялся — сверяем баланс
        // и подтягиваем результат из лога, чтобы приз не потерялся молча.
        if (!unauthorized) {
            try { await loadUserData(); } catch (e) {}
            checkPendingUpgradeResult();
        }
        return;
    }

    // Сервер присылает won (он же success). Основной флаг — won, success оставляем запасным.
    const won = data.won === undefined ? Boolean(data.success) : Boolean(data.won);
    // Шанс, с которым реально играли, — это data.chance с сервера. Клиентский
    // расчёт может разойтись с ним (округления, множитель), и тогда стрелка
    // останавливалась внутри сектора, хотя сервер бросил проигрыш.
    const roundChance = upgradeEffectiveChance(data, chance) || chance;
    lockUpgradeChance(roundChance);
    landUpgradeArrow(won, roundChance, remaining, token, () => {
        if (circle) circle.classList.remove('spinning');
        if (token !== upgradeSpinToken) return;
        finishUpgradeAnimation(data, roundChance);
    }, data.roll);
}

// Задача 70: id последнего отправленного раунда. Нужен, если ответ потерялся:
// «Сервер не ответил» больше не значит «ставка сгорела».
let lastUpgradeRequestId = null;

// Догнать уже сыгранный раунд по request_id. Сервер не списывает ставку
// повторно — он возвращает результат, который уже записан в журнале.
async function fetchPlayedUpgrade(requestId) {
    if (!requestId) return null;
    try {
        const res = await fetch(`${API_URL}/api/upgrade_action`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, replay_of: requestId })
        });
        if (!res.ok) return null;
        const data = await res.json().catch(() => null);
        return data && typeof data === 'object' ? data : null;
    } catch (e) {
        return null;
    }
}

// Единая точка применения ответа сервера: баланс и инвентарь берём только отсюда,
// чтобы любая ветка (успех, отмена раунда, восстановление из лога) давала одно и то же.
function applyUpgradeServerState(data) {
    if (!data) return false;
    // Серверное состояние — единственная правда: раунд закрыт, ограничение снято.
    clearUpgradeBalanceLock();
    upgradeStakePreview = null;      // пришло серверное состояние — превью не нужно
    const nextBalance = Number(data.new_balance);
    if (Number.isFinite(nextBalance)) {
        balance = nextBalance;
        balanceKnown = true;
    }
    const nextInventory = Array.isArray(data.new_inventory) ? data.new_inventory
        : (Array.isArray(data.inventory) ? data.inventory : null);
    if (nextInventory) inventory = nextInventory;
    return true;
}

async function ackUpgradeLog(logId) {
    if (!tg.initData) return;
    const body = JSON.stringify({
        initData: tg.initData,
        log_id: Number.isFinite(Number(logId)) ? Number(logId) : null
    });
    // Две попытки: если отметка не дойдёт, тот же раунд снова всплывёт как
    // «пропущенный», и игрок примет его за результат новой ставки.
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const res = await fetch(`${API_URL}/api/upgrade_ack`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body
            });
            if (res.ok) return;
        } catch (e) { /* попробуем ещё раз */ }
        if (attempt === 0) await new Promise(r => setTimeout(r, 1200));
    }
}

// Статистика апгрейдов живёт на сервере: иначе счётчики сбрасывались при каждом открытии.
async function loadUpgradeHistory() {
    if (!tg.initData) return null;
    try {
        const res = await fetch(`${API_URL}/api/upgrade_history`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        if (!res.ok) return null;
        const data = await res.json();
        if (data && data.stats) {
            upgradeStats.total = Number(data.stats.total) || 0;
            upgradeStats.wins = Number(data.stats.wins) || 0;
            upgradeStats.best = Number(data.stats.best) || 0;
            updateUpgradeStatsUI();
        }
        return data;
    } catch (e) {
        return null;
    }
}

// Показываем результат, который сервер уже выплатил, но игрок его не видел
// (закрыл приложение, таймаут запроса, перезапуск анимации).
function showMissedUpgradeResult(row, liveCoins) {
    if (!row || upgradeBusy) return false;
    const rowId = Number(row.id);
    if (Number.isFinite(rowId)) {
        if (upgradeShownLogIds.has(rowId)) return false;
        upgradeShownLogIds.add(rowId);
    }
    const won = Boolean(row.won);
    const chance = Number(row.chance) || 0;
    lockUpgradeChance(chance);
    // Задача 60: баланс берём из живого профиля (сервер отдаёт его в coins),
    // а coins_after из журнала — только запасной вариант. Иначе «прошлый раунд»
    // с недописанным журналом показывал 0 вместо настоящего баланса.
    applyUpgradeServerState({
        new_balance: Number.isFinite(liveCoins) ? liveCoins : row.coins_after,
    });

    const circle = document.getElementById('up-circle');
    if (circle) {
        circle.classList.remove('spinning', 'win', 'lose');
        circle.classList.add(won ? 'win' : 'lose');
        setUpgradeRingPercent(chance);
    }
    setUpgradeArrowAngle(((upgradeArrowTargetFor(won, chance, row.roll) % 360) + 360) % 360);
    const chanceEl = document.getElementById('up-chance');
    if (chanceEl) chanceEl.innerText = chance.toFixed(2) + '%';
    const btn = document.getElementById('upgrade-btn');
    if (btn) {
        btn.disabled = false;
        btn.classList.remove('win', 'lose');
        btn.innerText = 'ИГРАТЬ ЗАНОВО';
    }
    upgradeAwaitingRestart = true;
    releaseUpgradeBusy();
    setUpgradeNetFromResult(won, row);
    // Обязательная пометка: это НЕ результат текущей ставки, а прошлый раунд,
    // который игрок не увидел. Без неё легко решить, что приз «не засчитали».
    const prefix = '🕓 Прошлый раунд: ';
    if (won) {
        const text = row.win_item === 'coin'
            ? `${prefix}✅ выплата ${coinAmount(row.win_amount)}`
            : `${prefix}✅ ${upgradeItemEmoji(row.win_item)} ${upgradeItemName(row.win_item)} x${row.win_amount}`;
        setUpgradeResult(text, 'win');
    } else {
        setUpgradeResult(row.item_from === 'coin'
            ? `${prefix}❌ Не повезло — ставка сгорела`
            : `${prefix}❌ Не повезло — капсулы сгорели`, 'lose');
    }
    updateUI();
    ackUpgradeLog(row.id);
    return true;
}

async function checkPendingUpgradeResult() {
    if (upgradeBusy) return false;
    const data = await loadUpgradeHistory();
    if (!data || !data.available || !data.last_unseen) return false;
    // coins — живой баланс профиля из /api/upgrade_history (задача 60).
    return showMissedUpgradeResult(data.last_unseen, Number(data.coins));
}

// Результат уже посчитан сервером: здесь только показываем его игроку.
function finishUpgradeAnimation(data, chance) {
    if (!data) return;
    releaseUpgradeBusy();
    const circle = document.getElementById('up-circle');
    const btn = document.getElementById('upgrade-btn');
    // Сервер присылает won (он же success). Основной флаг — won, success оставляем запасным.
    const won = data.won === undefined ? Boolean(data.success) : Boolean(data.won);

    applyUpgradeServerState(data);

    upgradeStats.total += 1;
    if (won) upgradeStats.wins += 1;
    const rewardCoins = data.win_item === 'coin' ? Number(data.win_amount) || 0 : 0;
    const rewardItems = data.win_item && data.win_item !== 'coin' ? Number(data.win_amount) || 0 : 0;
    const rewardValue = rewardCoins || (rewardItems ? upgradePrice(data.win_item) * rewardItems : 0);
    if (won && rewardValue > upgradeStats.best) upgradeStats.best = rewardValue;

    if (circle) {
        circle.classList.remove('spinning', 'win', 'lose');
        circle.classList.add(won ? 'win' : 'lose');
        setUpgradeRingPercent(chance);
    }
    // Финальная сверка: стрелка обязана стоять в зоне вердикта. Если анимацию
    // прервали (быстрый повторный клик, уход с экрана) — доводим её сами тем же
    // расчётом, что и при прокрутке.
    if (data.roll != null && document.getElementById('up-arrow')) {
        setUpgradeArrowAngle(((upgradeArrowTargetFor(won, chance, data.roll) % 360) + 360) % 360);
    }
    const chanceEl = document.getElementById('up-chance');
    if (chanceEl) chanceEl.innerText = (Number(chance) || 0).toFixed(2) + '%';

    if (btn) {
        // Кнопка сразу готова к следующему раунду: синяя, с обычной надписью.
        // Раньше она оставалась заблокированной «ПОБЕДА/ПРОИГРЫШ» навсегда —
        // новую ставку можно было сделать только перезайдя в игру.
        btn.disabled = false;
        btn.classList.remove('win', 'lose');
        btn.innerText = 'ИГРАТЬ ЗАНОВО';
    }
    upgradeAwaitingRestart = true;

    setUpgradeNetFromResult(won, data);

    // Диагностика честности: вердикт сервера обязан совпадать с броском.
    // Сравниваем с тем шансом, по которому сервер считал вердикт (real_chance),
    // и заодно проверяем, что стрелка действительно в секторе успеха.
    const rollNumber = Number(data.roll);
    const chanceNumber = Number(chance);
    const expectedWon = upgradeRollWon(rollNumber, chanceNumber);
    if (expectedWon !== null && won !== expectedWon) {
        console.error('Апгрейд: бросок и вердикт расходятся', {
            roll: rollNumber, chance: chanceNumber, visualChance: data.chance, won,
        });
    }
    const insideSector = upgradeRollInsideSector(rollNumber, chanceNumber);
    if (insideSector !== null && insideSector !== won) {
        console.error('Апгрейд: стрелка и вердикт расходятся', {
            roll: rollNumber, chance: chanceNumber,
            angle: upgradeAngleForPercent(rollNumber),
            sectorDeg: upgradeAngleForPercent(chanceNumber), won,
        });
    }

    if (won) {
        // Ровно один аккуратный текст под кнопкой: «✅ Успех! Выплата X».
        const text = data.win_item === 'coin'
            ? `✅ Успех! Выплата ${coinAmount(data.win_amount)}`
            : `✅ Успех! Получено: ${upgradeItemEmoji(data.win_item)} ${upgradeItemName(data.win_item)} x${data.win_amount}`;
        setUpgradeResult(text, 'win');
        // Огромную всплывающую цифру здесь больше не показываем: она накрывала
        // круг и шанс. Итог виден строкой «✅ Успех! Выплата X» под кнопкой.
        playSound('win');
        try { tg.HapticFeedback.notificationOccurred('success'); } catch (e) {}
    } else {
        // Красный текст под кругом — ровно «❌ Не повезло».
        // Что именно сгорело (коины/капсулы и сколько), видно в блоке под кругом
        // (.up-net), поэтому дублировать это здесь больше не нужно.
        setUpgradeResult('❌ Не повезло', 'lose');
        playSound('lose');
        try { tg.HapticFeedback.notificationOccurred('error'); } catch (e) {}
    }

    updateUI();
    // Помечаем результат просмотренным, чтобы он не выскочил снова при входе.
    ackUpgradeLog(data.log_id);
}

// ================= МИНЫ =================
// Позиции мин, кэф и выплату считает сервер (/api/mines_*), клиент только рисует ответ.
// Так надбавка к риску первого клика и защита от двойного «Забрать» живут на сервере.
let minesActive = false, minesCoef = 1.0, minesSafeOpened = 0, selectedMines = 1, minesBet = 0;
const MINES_CELLS = 16;
const board = document.getElementById('mines-board');
if (board) {
    for (let i = 0; i < MINES_CELLS; i++) { const cell = document.createElement('div'); cell.className = 'cell'; cell.dataset.index = i; cell.onclick = () => clickMineCell(cell); board.appendChild(cell); }
}

function selectMines(count, element) {
    if (minesActive) return;
    selectedMines = count;
    document.querySelectorAll('.mine-btn').forEach(btn => btn.classList.remove('active'));
    if (element) element.classList.add('active');
}

function minesButton() { return document.getElementById('mines-btn'); }
function setMinesButton(html, background, disabled) {
    const btn = minesButton();
    if (!btn) return;
    btn.innerHTML = html;
    if (background) btn.style.background = background;
    btn.disabled = Boolean(disabled);
}
function updateMinesInfo(coef, cashout) {
    const coefEl = document.getElementById('mines-current-coef');
    const cashEl = document.getElementById('mines-cashout-amount');
    if (coefEl) coefEl.innerText = `x${Number(coef || 1).toFixed(2)}`;
    if (cashEl) cashEl.innerHTML = coinAmount(cashout || 0);
}
// Алмаз в безопасной клетке рисуем SVG, а не эмодзи 💎: на части устройств
// эмодзи приезжает с синим квадратом-подложкой, и открытая клетка выглядела
// как «сломанная иконка». Векторный кристалл одинаковый везде.
const MINES_GEM_SVG = `<svg class="cell-gem" viewBox="0 0 32 32" aria-hidden="true">
    <polygon points="8,6 24,6 30,13 16,29 2,13" fill="#bfefff" stroke="rgba(255,255,255,0.9)" stroke-width="1.2" stroke-linejoin="round"/>
    <polygon points="8,6 24,6 21,13 11,13" fill="#eaffff" opacity="0.95"/>
    <polygon points="2,13 11,13 16,29" fill="#7fd8ff"/>
    <polygon points="21,13 30,13 16,29" fill="#4bb6f5"/>
    <polygon points="11,13 21,13 16,29" fill="#9fe6ff"/>
</svg>`;

function resetMinesCells() {
    document.querySelectorAll('.cell').forEach(c => {
        c.className = 'cell'; c.innerHTML = ''; c.style.pointerEvents = 'auto';
    });
}
function lockMinesCells() {
    document.querySelectorAll('.cell').forEach(c => { c.style.pointerEvents = 'none'; });
}
function revealMines(positions, hitIndex) {
    (positions || []).forEach(index => {
        const cell = board && board.children[index];
        if (!cell) return;
        cell.classList.add('flipped', 'bad');
        cell.innerHTML = index === hitIndex ? '💥' : '💣';
    });
}
function resetMinesRound() {
    minesActive = false;
    minesCoef = 1.0; minesSafeOpened = 0; minesBet = 0;
    lockMinesCells();
    setMinesButton('Сделать ставку', '#007aff', false);
    updateMinesInfo(1, 0);
}

async function minesAction() {
    if (isProcessing) return;
    if (!userDataLoaded) {
        await loadUserData();
        if (!userDataLoaded) return tg.showAlert("Не удалось загрузить баланс. Проверьте соединение и попробуйте снова.");
    }
    if (minesActive) return cashoutMines();
    return startMines();
}

async function startMines() {
    if (isProcessing) return;
    const bet = getBet('mines');
    if (bet < 10) return tg.showAlert("Минимальная ставка 10 коинов");
    if (balance < bet) return tg.showAlert("Недостаточно средств!");
    if (!isAdminId(user && user.id) && bet > 10000) return tg.showAlert("Максимальная ставка 10 000 коинов");

    isProcessing = true;
    setMinesButton('Загрузка...', '#555', true);
    try {
        const res = await fetch(`${API_URL}/api/mines_start`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, amount: bet, mines: selectedMines,
                                   request_id: requestId() })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { tg.showAlert(data.detail || "Не удалось начать игру"); return; }
        balance = Number(data.new_balance); balanceKnown = true; updateUI();
        minesActive = true; minesCoef = 1.0; minesSafeOpened = 0; minesBet = bet;
        currentBet = bet;
        resetMinesCells();
        updateMinesInfo(1, bet);
        setMinesButton(`Забрать (${coinAmount(bet)})`, '#34c759', false);
        playSound('click');
    } catch (e) {
        tg.showAlert("Ошибка соединения. Ставка не принята, попробуйте ещё раз.");
    } finally {
        isProcessing = false;
        if (!minesActive) setMinesButton('Сделать ставку', '#007aff', false);
    }
}

async function cashoutMines() {
    if (isProcessing) return;
    isProcessing = true;
    // Блокируем кнопку сразу: повторный клик физически невозможен, пока идёт запрос.
    setMinesButton('Забираем...', '#555', true);
    let paid = false;
    try {
        const res = await fetch(`${API_URL}/api/mines_cashout`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            tg.showAlert(data.detail || "Не удалось забрать выигрыш");
        } else {
            balance = Number(data.new_balance); balanceKnown = true; updateUI();
            paid = true;
            showWin(`+ ${coinAmount(data.win_amount)}`);
            playSound('win');
        }
    } catch (e) {
        tg.showAlert("Ошибка соединения. Попробуйте забрать ещё раз.");
    } finally {
        isProcessing = false;
        if (paid) {
            minesActive = false;
            // Сразу возвращаем кнопку в «Сделать ставку» — никакого второго начисления.
            resetMinesRound();
        } else {
            // Не удалось — возвращаем зеленую кнопку, чтобы игрок мог попробовать снова.
            setMinesButton(`Забрать (${coinAmount(Math.floor(minesBet * minesCoef))})`, '#34c759', false);
        }
    }
}

async function clickMineCell(cell) {
    if (!minesActive || isProcessing) return;
    const index = parseInt(cell.dataset.index);
    if (Number.isNaN(index) || cell.classList.contains('flipped')) return;

    isProcessing = true;
    // Задача 50: клетка НЕ «висит» на анимации. Раньше ответ ждали до 0.4 с
    // анимации переворота, а при медленном сервере — ещё дольше, и игра
    // казалась тормозной. Теперь запрос уходит сразу, ответ рисуется, как
    // только пришёл, а спиннер появляется лишь если сервер молчит >180 мс.
    cell.classList.add('flipped');
    playSound('click');
    const slowTimer = setTimeout(() => cell.classList.add('loading'), 180);
    try {
        const res = await fetch(`${API_URL}/api/mines_open`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, cell: index })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            cell.classList.remove('flipped', 'loading');
            tg.showAlert(data.detail || "Не удалось открыть клетку");
            return;
        }
        cell.classList.remove('loading');
        if (data.hit) {
            cell.classList.add('bad');
            revealMines(data.positions, index);
            playSound('boom');
            minesActive = false;
            lockMinesCells();
            setMinesButton('Сделать ставку', '#007aff', false);
            updateMinesInfo(1, 0);
            return;
        }
        cell.classList.add('good');
        cell.innerHTML = MINES_GEM_SVG;
        minesSafeOpened = Number(data.opened_count) || 0;
        minesCoef = Number(data.coef) || 1;
        const cashout = Number(data.cashout) || 0;
        updateMinesInfo(minesCoef, cashout);
        setMinesButton(`Забрать (${coinAmount(cashout)})`, '#34c759', false);
    } catch (e) {
        cell.classList.remove('flipped', 'loading');
        tg.showAlert("Ошибка соединения. Клетка не открыта.");
    } finally {
        clearTimeout(slowTimer);
        isProcessing = false;
    }
}

// ================= АРЕНА (ХОККЕЙ) =================
// Минимальная ставка арены: то же число, что ARENA_MIN_BET в api.py.
const ARENA_MIN_BET_CLIENT = 100;

async function placeHockeyBet(team) {
    if (isProcessing) return;
    const input = document.getElementById('hockey-bet');
    // Задача 56: минимум арены — 100. Задача 58: НЕ подставляем его молча.
    // Раньше `Math.max(100, 10)` превращало введённые 10 в 100 и выглядело как
    // «ставлю 10, оно ставит 100». Теперь показываем, что не так, и не отправляем.
    const entered = parseInt(input && input.value);
    const bet = Number.isFinite(entered) ? entered : 0;
    if (bet < ARENA_MIN_BET_CLIENT) {
        markArenaBetTooLow('hockey', bet);
        return tg.showAlert(`Минимальная ставка ${ARENA_MIN_BET_CLIENT} коинов. `
            + `Вы ввели ${bet > 0 ? bet : 'пустое поле'} — уменьшить минимум нельзя.`);
    }
    markArenaBetTooLow('hockey', bet);
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");
    if (!isAdminId(user && user.id) && bet > 10000) return tg.showAlert("Максимальная ставка 10 000 коинов");

    isProcessing = true;
    try {
        // team передаём только в командном режиме: сервер сам проверит, что
        // игрок не ставит за обе команды в одном раунде.
        // Арена ставки: командная (если нажата красная/синяя кнопка) или та,
        // что открыта. Котлы независимы, поэтому в одном раунде можно
        // поставить и в обычную, и в командную арену.
        const payload = { amount: bet, request_id: requestId(), arena: team ? 'team' : arenaState.view };
        if (team) payload.team = team;
        const data = await apiPost('/api/place_bet', payload, { fallback: "Ставки закрыты!" });
        balance = data.new_balance; balanceKnown = true; updateUI(); playSound('click');
        if (team) { arenaState.myTeam = team; updateArenaModeUI(arenaState.view, team); }
        pollHockeyState();
    } catch (e) {
        tg.showAlert(e.message || "Ошибка соединения");
    }
    isProcessing = false;
}

const HOCKEY_ROLL_DURATION = 9.0;
// Шаг симуляции мяча на сервере (api.py): 0.05 с на точку траектории.
const HOCKEY_SIMULATION_STEP = 0.05;

// Доля круга (проценты) → градусы. 0° — верх круга, по часовой стрелке.
function arenaAngleForShare(percent) {
    return Math.max(0, Math.min(360, (Number(percent) || 0) * 3.6));
}

function arenaShapes(state) {
    const sectors = (state && Array.isArray(state.sectors) && state.sectors.length)
        ? state.sectors
        : ((state && Array.isArray(state.players)) ? state.players : []);
    return sectors.filter(item => Number(item.bet) > 0);
}

// ================= ОБЩЕЕ ДЛЯ ОБОИХ АРЕН =================
// Задача 29: главный баг «иконки дёргаются, анимаций нет» — арена
// перерисовывалась ЦЕЛИКОМ каждые 200 мс (опрос состояния). Каждый новый
// элемент заново запускал CSS-анимацию появления, поэтому вместо анимации
// был рывок. Теперь у обеих арен есть «подпись» состояния: если игроки и
// ставки не изменились, DOM не трогаем вообще.
function arenaSignature(list, extra) {
    return list.map(item => [
        item.id, item.bet, item.color, item.team || '', item.avatar || '',
        (item.name || '').length,
    ].join(':')).join('|') + '||' + (extra || '');
}

// ================= КОМАНДНАЯ АРЕНА: КРУГ =================
const arenaCircleCache = { signature: null };

// Рисует круг: conic-gradient из секторов (размер = доля ставки) + аватарки
// внутри каждого сектора. Чем больше ставка — тем больше сектор и аватарка.
function renderArenaCircle(state) {
    const pie = document.getElementById('arena-pie');
    const chips = document.getElementById('arena-players');
    if (!pie || !chips) return;

    const sectors = arenaShapes(state);
    if (!sectors.length) {
        pie.style.background = 'radial-gradient(circle at 50% 50%, #1c1c1c 0 62%, #0f0f0f 63%)';
        if (arenaCircleCache.signature !== 'empty') {
            arenaCircleCache.signature = 'empty';
            chips.innerHTML = '<span class="arena-empty">Ждём ставок…</span>';
        }
        return;
    }

    const finalX = state && state.status === 'finished' && state.roll_data
        ? Number(state.roll_data.final_x) : null;
    const winnerId = state && state.winner_team
        ? state.winner_team
        : (state && state.winner ? String(state.winner.id) : null);
    const signature = arenaSignature(sectors, `${winnerId || ''}:${state && state.status}`);
    if (signature === arenaCircleCache.signature) return;   // ничего не изменилось
    arenaCircleCache.signature = signature;

    const totalBet = sectors.reduce((sum, sector) => sum + Math.max(0, Number(sector.bet) || 0), 0) || 1;
    let cursor = 0;
    const stops = [];
    const geometry = [];
    sectors.forEach(sector => {
        const share = Math.max(0, Number(sector.bet) || 0) / totalBet * 100;
        const start = cursor;
        const end = Math.min(100, cursor + share);
        const color = sector.color || (sector.id === 'red' ? '#ff3b30' : sector.id === 'blue' ? '#007aff' : '#5856d6');
        stops.push(`${color} ${arenaAngleForShare(start).toFixed(2)}deg ${arenaAngleForShare(end).toFixed(2)}deg`);
        geometry.push({ sector, start, end, share, color });
        cursor = end;
    });
    pie.style.background = `conic-gradient(from 0deg, ${stops.join(', ')})`;
    pie.classList.toggle('settled', finalX !== null || Boolean(winnerId));

    chips.innerHTML = geometry.map(item => {
        const mid = (item.start + item.end) / 2;               // процент круга
        const angle = arenaAngleForShare(mid) * Math.PI / 180; // радианы от верха
        // Чип ставим на 33% радиуса: внутри своего сектора, но не в центре.
        const radius = 0.33;
        const left = 50 + Math.sin(angle) * radius * 100;
        const top = 50 - Math.cos(angle) * radius * 100;
        const size = Math.max(26, Math.min(46, 20 + item.share * 0.62));
        const name = item.sector.name || item.sector.username || 'Игрок';
        const isWinner = Boolean(winnerId) && (String(item.sector.id) === String(winnerId)
            || (item.sector.is_team && String(item.sector.id) === String(state.winner_team)));
        const avatar = !item.sector.is_team && item.sector.avatar && String(item.sector.avatar).startsWith('http')
            ? `<img src="${escapeHtml(item.sector.avatar)}" alt="">`
            : `<span class="arena-chip-letter">${escapeHtml(String(name).slice(0, 1).toUpperCase())}</span>`;
        const label = item.sector.is_team
            ? `${escapeHtml(name)} · ${item.share.toFixed(0)}%`
            : `${escapeHtml(shortName(name, 10))}`;
        return `<div class="arena-chip${isWinner ? ' win' : ''}" style="left:${left.toFixed(2)}%; top:${top.toFixed(2)}%; --chip-size:${size.toFixed(0)}px; --chip-color:${escapeHtml(item.color)};">
                    <span class="arena-chip-avatar">${avatar}</span>
                    <span class="arena-chip-label">${label}</span>
                </div>`;
    }).join('');
}

// ================= КОМАНДНАЯ АРЕНА: СТРЕЛКА =================
// Крутится как стрелка Апгрейда (ровное вращение 900°/с), а на финише
// ЗАМЕДЛЯЕТСЯ ПОСТЕПЕННО: скорость падает по экспоненте, поэтому в первую
// секунду стрелка летит почти с прежней скоростью, дальше всё медленнее и к
// концу почти замирает. Раньше финиш был одной CSS-кривой cubic-bezier — на
// пяти секундах она сбрасывала почти всё замедление в первые полсекунды, и
// выглядело так, будто стрелка резко тормозит, а потом ползёт.
// Задача 37: затухание считаем на каждом кадре (requestAnimationFrame).
const ARENA_POINTER_SPEED = 900;          // °/с — как UPGRADE_ARROW_SPEED
const ARENA_POINTER_LAND_MS = 5000;       // длительность финишного доезда
// Задача 42: прокрут как в апгрейде — РАЗГОН (скорость растёт постепенно),
// потом ТОРМОЖЕНИЕ, причём на последней секунде стрелка уже еле ползёт.
// Задача 44: весь бросок сокращён до 5–6 секунд — 2 с разгон + 3.5 с торможение
// = 5.5 с. Раньше было 8 с, и игроки жаловались, что «слишком долго крутится».
const ARENA_SPIN_UP_MS = 2000;
const ARENA_SPIN_DOWN_MS = 3500;
const ARENA_TEAM_ROLL_MS = ARENA_SPIN_UP_MS + ARENA_SPIN_DOWN_MS;
// Доля пути за разгон при скорости, растущей как 1 − e^(−3u): площадь под
// такой кривой на отрезке u∈[0,1].
const ARENA_SPIN_UP_AREA = 1 - (1 - Math.exp(-3)) / 3;

function arenaNowMs() {
    return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
}

// Путь, который стрелка проходит за 3 секунды разгона (≈1844°, т.е. ~5 оборотов).
function arenaSpinUpDistance() {
    // Оборотов должно быть столько же, сколько раньше: короткий бросок не
    // должен выглядеть «ленивым» — скорость подстраивается под новую длину.
    return ARENA_POINTER_SPEED * (ARENA_SPIN_UP_MS / 1000) * ARENA_SPIN_UP_AREA;
}

// Нормированный пройденный путь за разгон: 0 → 1, скорость в конце ~0.95 полной.
function arenaSpinUpProgress(u) {
    if (!(u > 0)) return 0;
    if (u >= 1) return 1;
    return (u - (1 - Math.exp(-3 * u)) / 3) / ARENA_SPIN_UP_AREA;
}
// Резерв, если в WebView нет requestAnimationFrame: тот же доезд через CSS.
const ARENA_POINTER_LAND_EASING = 'cubic-bezier(0.1, 0.7, 0.1, 1)';
// Победителя показываем только ПОСЛЕ того, как стрелка встала / мяч замер.
const ARENA_WINNER_DELAY_MS = 1200;        // обычная арена: мяч уже замер
// Задача 43: после остановки стрелки даём ПАУЗУ 1.6 с — игрок должен увидеть,
// на каком цвете она встала, и только потом выезжает плашка результата.
// Раньше плашка появлялась почти сразу, и «торможения» никто не замечал.
const ARENA_WINNER_AFTER_LAND_MS = 1100;

function arenaPointerNode() { return document.getElementById('arena-pointer'); }

// Угол ставится мгновенно (фаза вращения и сброс); плавный финиш включает
// transition сам и зовёт этот хелпер с keepTransition.
function arenaPointerSetAngle(deg, keepTransition) {
    arenaPointer.angle = deg;
    const pointer = arenaPointerNode();
    if (!pointer) return;
    if (!keepTransition) pointer.style.transition = 'none';
    pointer.style.transform = `rotate(${deg.toFixed(2)}deg)`;
}

function arenaPointerStopSpin() {
    arenaPointer.spinning = false;
    if (arenaPointer.rafId) cancelAnimationFrame(arenaPointer.rafId);
    arenaPointer.rafId = null;
    const pointer = arenaPointerNode();
    if (pointer) pointer.classList.remove('spinning');
}

function arenaResetPointer() {
    arenaPointerStopSpin();
    arenaPointer.landToken = (arenaPointer.landToken || 0) + 1;   // отменяем незавершённый финиш
    arenaPointer.angle = 0;
    arenaPointer.landedRound = null;
    const pointer = arenaPointerNode();
    if (pointer) {
        pointer.style.transition = 'none';
        pointer.style.transform = 'rotate(0deg)';
        pointer.classList.remove('landing');
    }
}

// Сколько градусов набежит при скорости v(t) = v0·e^(−k·t) за seconds секунд.
function arenaDecayCoverage(v0, k, seconds) {
    if (!(k > 0)) return v0 * seconds;
    return (v0 / k) * (1 - Math.exp(-k * seconds));
}

// Подбираем коэффициент затухания k так, чтобы за totalMs набежало ровно
// distance градусов: стрелка стартует со скорости вращения и плавно гаснет.
function arenaDecayFactor(distance, totalMs, v0) {
    const seconds = Math.max(0.1, Number(totalMs) / 1000);
    let lo = 0.01, hi = 40;
    for (let i = 0; i < 60; i++) {
        const mid = (lo + hi) / 2;
        if (arenaDecayCoverage(v0, mid, seconds) > distance) lo = mid;
        else hi = mid;
    }
    return (lo + hi) / 2;
}

// Финиш: минимум четыре полных оборота + доводка до сектора победителя,
// с постепенным замедлением «с каждой секундой медленнее».
// Токен защищает от гонки: новый раунд сбрасывает стрелку, а старый финиш
// не должен ничего «дорисовать».
function arenaLandPointer(finalX, durationMs) {
    const pointer = arenaPointerNode();
    arenaPointerStopSpin();
    if (!pointer) return;
    const token = (arenaPointer.landToken || 0) + 1;
    arenaPointer.landToken = token;

    const desired = ((arenaAngleForShare(Math.max(0, Math.min(100, Number(finalX) || 0))) % 360) + 360) % 360;
    const current = ((arenaPointer.angle % 360) + 360) % 360;
    const delta = ((desired - current) % 360 + 360) % 360;
    const total = Math.max(2200, Number(durationMs) || ARENA_POINTER_LAND_MS);
    const turns = Math.max(4, Math.ceil(total / 1000));
    const distance = turns * 360 + delta;
    const startAngle = arenaPointer.angle;

    let finished = false;
    const finish = () => {
        if (finished) return;
        finished = true;
        if (token !== arenaPointer.landToken) return;
        arenaPointerSetAngle(startAngle + distance);        // фиксируем угол
    };

    if (typeof requestAnimationFrame !== 'function') {
        // Резервный путь: один CSS-переход (старые WebView без rAF).
        arenaPointerSetAngle(startAngle);
        void pointer.offsetWidth;
        pointer.classList.add('landing');
        pointer.style.transition = `transform ${total}ms ${ARENA_POINTER_LAND_EASING}`;
        arenaPointer.angle = startAngle + distance;
        pointer.style.transform = `rotate(${(startAngle + distance).toFixed(2)}deg)`;
        setTimeout(finish, total + 300);
        return;
    }

    pointer.classList.add('landing');
    pointer.style.transition = 'none';          // угол двигаем вручную, кадр за кадром
    const v0 = ARENA_POINTER_SPEED;             // старт — со скорости вращения
    const k = arenaDecayFactor(distance, total, v0);
    const norm = 1 - Math.exp(-k * (total / 1000));
    const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
    const startedAt = nowMs();

    const step = () => {
        if (finished || token !== arenaPointer.landToken) return;
        const elapsed = Math.max(0, nowMs() - startedAt);
        const progress = Math.min(1, elapsed / total);
        // Задача 42: к концу торможения стрелка должна ПОЛЗТИ, а не «доезжать
        // рывком». Чистая экспонента к последней секунде оставляет заметный
        // хвост, поэтому складываем две кривые: экспоненту (быстрый сброс
        // скорости в начале) и кубическое сглаживание (мягкий, почти нулевой
        // финал). Так на 7-й секунде из 8-ми стрелка делает лишь несколько
        // градусов и останавливается ровно на секторе победителя.
        const expo = norm > 0 ? (1 - Math.exp(-k * (progress * total / 1000))) / norm : progress;
        const easeOut = 1 - Math.pow(1 - progress, 3);
        const factor = Math.min(1, expo * 0.55 + easeOut * 0.45);
        arenaPointerSetAngle(startAngle + distance * factor);
        if (progress >= 1) return finish();
        requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    // Страховка: если кадры не приходят (окно свернули), фиксируем финал.
    setTimeout(finish, total + 500);
}

function updateArenaModeUI(mode, myTeam) {
    const isTeam = mode === 'team';
    const normalBtn = document.getElementById('arena-mode-normal');
    const teamBtn = document.getElementById('arena-mode-team');
    if (normalBtn) normalBtn.classList.toggle('active', !isTeam);
    if (teamBtn) teamBtn.classList.toggle('active', isTeam);
    const teamRow = document.getElementById('arena-team-buttons');
    const singleBtn = document.getElementById('hockey-btn');
    // В обычной арене красных/синих нет вообще: строка скрыта и скрыта
    // физически (display), чтобы её нельзя было «достать» стилями браузера.
    if (teamRow) {
        teamRow.hidden = !isTeam;
        teamRow.style.display = isTeam ? 'flex' : 'none';
        teamRow.setAttribute('aria-hidden', isTeam ? 'false' : 'true');
    }
    if (singleBtn) singleBtn.style.display = isTeam ? 'none' : '';
    const redBtn = document.getElementById('team-red-btn');
    const blueBtn = document.getElementById('team-blue-btn');
    const team = myTeam || arenaState.myTeam;
    // Нельзя ставить за обе команды: «чужая» кнопка блокируется до конца раунда.
    if (redBtn) redBtn.disabled = team === 'blue';
    if (blueBtn) blueBtn.disabled = team === 'red';
    if (redBtn) redBtn.classList.toggle('picked', team === 'red');
    if (blueBtn) blueBtn.classList.toggle('picked', team === 'blue');
}

// ================= ДВЕ АРЕНЫ ОДНОВРЕМЕННО (задача 31) =================
// Арены живут на сервере параллельно, поэтому переключение — чисто локальное:
// ни запроса, ни ожидания конца раунда, ни запрета при живой ставке.
function arenaPool(view) {
    const pools = arenaState.arenas || {};
    return pools[view === 'team' ? 'team' : 'normal']
        || { players: [], sectors: [], teams: [], bank: 0, winner: null, winner_team: null };
}

// Снимок обеих арен из ответа /api/hockey_state. Плоские поля оставлены как
// запасной путь: если сервер старой сборки, клиент всё равно не падает.
function arenaPoolsFromState(state) {
    // Задача 42: у каждой арены СВОЙ раунд (статус, таймер, round_id), поэтому
    // клиент больше не берёт статус «сверху» — он у каждого котла собственный.
    const arenasFromServer = (state.arenas && typeof state.arenas === 'object') ? state.arenas : {};
    const normalServer = arenasFromServer.normal || {};
    const teamServer = arenasFromServer.team || {};
    const normal = {
        id: 'normal',
        players: Array.isArray(state.normal_players) ? state.normal_players
            : (Array.isArray(state.players) ? state.players : []),
        sectors: Array.isArray(state.sectors) ? state.sectors : [],
        bank: Number(state.normal_bank != null ? state.normal_bank : state.bank) || 0,
        winner: state.winner || null,
        winner_team: null,
        status: normalServer.status || state.normal_status || state.status || 'waiting',
        // Задача 47: закрыт ли раунд по деньгам ('pending' → деньги ещё идут).
        payout_status: normalServer.payout_status || 'none',
        payout_error: normalServer.payout_error || '',
        timer: Number(normalServer.timer != null ? normalServer.timer : state.normal_timer) || 0,
        round_id: String(normalServer.round_id || state.normal_round_id || state.round_id || ''),
        roll_duration: Number(normalServer.roll_duration) || Number(state.roll_duration) || 9,
    };
    const team = {
        id: 'team',
        players: Array.isArray(state.team_players) ? state.team_players : [],
        sectors: Array.isArray(state.team_sectors) ? state.team_sectors
            : (Array.isArray(state.teams) ? state.teams : []),
        teams: Array.isArray(state.teams) ? state.teams : [],
        bank: Number(state.team_bank) || 0,
        // Задача 47: победитель командной арены — объект КОМАНДЫ. Старый
        // ответ присылал только id в winner_team, поэтому плашку было нечем
        // заполнить; поддерживаем оба вида.
        winner: teamServer.winner
            || (state.arena_winners && state.arena_winners.team)
            || (teamServer.winner_team || state.winner_team
                ? { id: teamServer.winner_team || state.winner_team,
                    team: teamServer.winner_team || state.winner_team, is_team: true }
                : null),
        winner_team: teamServer.winner_team || state.winner_team
            || (teamServer.winner && (teamServer.winner.team || teamServer.winner.id)) || null,
        status: teamServer.status || state.team_status || 'waiting',
        payout_status: teamServer.payout_status || 'none',
        payout_error: teamServer.payout_error || '',
        // Задача 47: имя победителя — ТОЛЬКО своё. Раньше здесь стоял
        // normalServer.winner_name, и в командную арену попадал победитель
        // одиночной — та самая «плашка не из того режима».
        winner_name: teamServer.winner_name || '',
        timer: Number(teamServer.timer != null ? teamServer.timer : state.team_timer) || 0,
        round_id: String(teamServer.round_id || state.team_round_id || ''),
        roll_duration: Number(teamServer.roll_duration) || Number(state.team_roll_duration) || 8,
        teams_summary: teamServer.teams || state.teams || [],
    };
    const fromServer = state.arenas && typeof state.arenas === 'object' ? state.arenas : {};
    const normalMerged = Object.assign({}, normal, fromServer.normal || {});
    const teamMerged = Object.assign({}, team, fromServer.team || {});
    // Задача 47: поля победителя НЕ перетираем сырым блоком сервера. Старый
    // ответ присылал winner: null в командной арене, и Object.assign затирал
    // нормализованное значение — плашка «Победитель: команда …» не появлялась.
    normalMerged.winner = (fromServer.normal && fromServer.normal.winner) || normal.winner || null;
    teamMerged.winner = (fromServer.team && fromServer.team.winner) || team.winner || null;
    teamMerged.winner_team = (fromServer.team && fromServer.team.winner_team) || team.winner_team || null;
    return { normal: normalMerged, team: teamMerged };
}

function arenaSelfIn(view) {
    const me = String((user && user.id) || '');
    if (!me) return null;
    const players = arenaPool(view).players || [];
    return players.find(p => String(p.id) === me && !p.is_bot) || null;
}

// Пересобирает «активную» арену (ту, что смотрит игрок) из снимка обеих и
// перерисовывает поле/круг и список участников. Вызывается и из опроса, и при
// локальном переключении — поэтому переключение не ждёт сети.
function syncArenaView() {
    const view = arenaState.view === 'team' ? 'team' : 'normal';
    arenaState.view = view;
    arenaState.mode = view;
    arenaState.players = arenaPool(view).players || [];
    arenaState.sectors = arenaPool(view).sectors || [];
    arenaState.teams = arenaPool('team').teams || [];
    const mine = arenaSelfIn('team');
    arenaState.myTeam = mine ? (mine.team || null) : null;
    updateArenaModeUI(view, arenaState.myTeam);
    updateArenaView(view);
    renderArenaActive();
    renderArenaParticipants();
    const bankEl = document.getElementById('hockey-bank');
    if (bankEl) bankEl.innerHTML = `Банк: ${coinAmount(arenaPool(view).bank)}`;
}

// Список участников активной арены. Раньше он перерисовывался на каждом
// опросе (аватарки дёргались), теперь — только при изменении состава/ставок.
// Функция общая для опроса и локального переключения арены.
// Командная арена: две плитки по бокам — слева синие, справа красные.
// Раньше все участники шли одной лентой вниз, и понять, кто за кого, можно было
// только по эмодзи в нике. Теперь состав команд разложен по плиткам, как
// мини-таблица: своя команда подсвечена, победившая — золотом.
function renderTeamBoards() {
    const wrap = document.getElementById('arena-team-boards');
    const flat = document.getElementById('hockey-participants');
    if (!wrap) return;
    const pool = arenaPool('team');
    const players = Array.isArray(pool.players) ? pool.players : [];
    const banks = { red: 0, blue: 0 };
    players.forEach(p => {
        if (p.team === 'red' || p.team === 'blue') banks[p.team] += Math.max(0, Number(p.bet) || 0);
    });
    const winnerTeam = pool.winner_team || (pool.winner && pool.winner.id) || null;
    // Задача 56: банк делится на ВСЕ ставки победившей команды — вместе с
    // ботами (та же формула, что в api.py). Раньше знаменатель считался только
    // по живым игрокам, и ставка 1 коин при боте на 868 забирала весь банк.
    // Доля = банк * моя_ставка / ставки_команды — «ставка + процент».
    const teamTotal = (winnerTeam && banks[winnerTeam]) || 0;
    ['blue', 'red'].forEach(team => {
        const listEl = document.getElementById(`team-board-${team}-list`);
        const sumEl = document.getElementById(`team-board-${team}-sum`);
        const board = document.getElementById(`team-board-${team}`);
        if (!listEl) return;
        const members = players
            .filter(p => p.team === team)
            .sort((a, b) => (Number(b.bet) || 0) - (Number(a.bet) || 0));
        const total = banks[team] || 0;
        // Задача 44: в командной арене детализируем плитку — сколько игроков,
        // какой общий банк и сколько получит каждый при победе команды.
        const totalBank = (Number(pool.bank) || 0) || (banks.red + banks.blue);
        const signature = `${team}:${winnerTeam || ''}:${totalBank}:${teamTotal}:`
            + members.map(p => `${p.id}:${p.bet}:${p.is_bot ? 1 : 0}`).join(',');
        // coinAmount() отдаёт готовый HTML (внутри картинка монеты), поэтому
        // только innerHTML — иначе на плитке видно "<img class=coin-img ...>".
        if (sumEl) {
            // Задача 56: вместо «ставок N» (игроки читали как «N игроков»)
            // показываем шанс команды забрать банк — это то, что видно на круге.
            const teamChance = (banks[team] && totalBank) ? Math.round(banks[team] / totalBank * 100) : 0;
            sumEl.innerHTML = `${coinAmount(total)}<span class="team-board-meta">`
                + (totalBank ? `шанс ${teamChance}%` : 'ждём ставок') + `</span>`;
        }
        if (board) board.classList.toggle('win', Boolean(winnerTeam) && winnerTeam === team);
        if (listEl.dataset.signature === signature) return;
        listEl.dataset.signature = signature;
        listEl.innerHTML = '';
        if (!members.length) {
            const empty = document.createElement('div');
            empty.className = 'arena-team-empty';
            empty.textContent = 'Ждём ставок…';
            listEl.appendChild(empty);
            return;
        }
        members.forEach(p => {
            const row = document.createElement('div');
            row.className = 'arena-team-row' + (String(p.id) === String(user && user.id) ? ' mine' : '');
            // Задача 56: ник больше не обрезается до 10 символов «в никуда» —
            // он занимает всю ширину строки, а лишнее прячет CSS-троеточие.
            const name = escapeHtml(String(p.name || '?'));
            const bet = Math.max(0, Number(p.bet) || 0);
            const percent = total ? (bet / total * 100).toFixed(0) : '0';
            const payout = (team === winnerTeam && teamTotal > 0)
                ? Math.round(totalBank * bet / teamTotal) : 0;
            // Задача 48: подписи «бот» больше нет — просто прочерк.
            const payoutHtml = p.is_bot
                ? `<span class="row-payout muted" title="Боты тоже делят банк — но их выплата не выводится">—</span>`
                : `<span class="row-payout${payout > bet ? '' : ' muted'}" `
                  + `title="Выплата, если команда победит (ставка + доля банка)">`
                  + `${payout > 0 ? coinAmount(payout) : '—'}</span>`;
            // Задача 56: две строки вместо одной. Раньше ставка, процент и
            // выплата стояли в той же строке, что ник, и на узкой плитке
            // цифра налезала на имя («ставка прямо на нике» со скрина).
            row.innerHTML = `<span class="row-name" title="${escapeHtml(String(p.name || ''))}">${name}</span>`
                + `<span class="row-meta">`
                + `<span class="row-bet" title="Ставка · доля в команде">${coinAmount(bet)} · ${percent}%</span>`
                + payoutHtml
                + `</span>`;
            listEl.appendChild(row);
        });
    });
    wrap.hidden = false;
    wrap.style.display = 'flex';
    if (flat) { flat.hidden = true; flat.style.display = 'none'; }
}

function renderFlatParticipants() {
    const wrap = document.getElementById('arena-team-boards');
    const flat = document.getElementById('hockey-participants');
    if (wrap) { wrap.hidden = true; wrap.style.display = 'none'; }
    if (flat) { flat.hidden = false; flat.style.display = 'flex'; }
}

function renderArenaParticipants() {
    // В командной арене — две плитки по командам, в обычной — прежняя лента.
    if (arenaState.view === 'team') {
        renderTeamBoards();
        return;
    }
    renderFlatParticipants();
    const list = document.getElementById('hockey-participants');
    if (!list) return;
    const players = arenaState.players || [];
    const poolBank = Number(arenaPool(arenaState.view).bank) || 0;
    const listSignature = arenaSignature(players, `${arenaState.view}:${poolBank}`);
    if (list.dataset.signature === listSignature) return;
    list.dataset.signature = listSignature;
    list.innerHTML = '';
    players.forEach((p, i) => {
        const div = document.createElement('div'); div.className = 'part-item';
        const width = Number(p.percent) || (poolBank ? (Number(p.bet) || 0) / poolBank * 100 : 0);
        let iconHtml = '';
        if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) { iconHtml = `<img src="${p.avatar}" style="width:20px; height:20px; border-radius:50%; margin-right:5px; vertical-align:middle;">`; }
        else { const letter = p.name ? p.name[0].toUpperCase() : '?'; iconHtml = `<span style="display:inline-flex; justify-content:center; align-items:center; width:20px; height:20px; border-radius:50%; background:${p.color}; color:white; font-size:10px; font-weight:bold; margin-right:5px;">${letter}</span>`; }
        const teamMark = p.team === 'red' ? '🔴 ' : p.team === 'blue' ? '🔵 ' : '';
        div.innerHTML = `<div class="name"><span>#${i+1}</span>${iconHtml}<span title="${escapeHtml(p.name)}">${teamMark}${escapeHtml(shortName(p.name))}</span></div><div class="stats">${width.toFixed(1)}% | ${coinAmount(p.bet)}</div>`;
        list.appendChild(div);
    });
}

function renderArenaActive() {
    const state = arenaState.last;
    if (!state) return;
    // Задача 42: котлы независимы — рисуем по статусу ТОЙ арены, что открыта.
    const pool = arenaPool(arenaState.view);
    const own = Object.assign({}, state, { status: pool.status || 'waiting' },
        arenaStateForView(state, arenaState.view));
    if (arenaState.view === 'team') {
        if (arenaShapes(pool).length || own.status === 'waiting') {
            renderArenaCircle(own);
        }
    } else {
        renderNormalArena(own);
    }
}

// Состояние одного котла в форме, которую ждут рендереры арен.
function arenaStateForView(state, view) {
    const pool = arenaPool(view);
    return Object.assign({}, state, {
        players: pool.players || [],
        sectors: pool.sectors || [],
        teams: view === 'team' ? (pool.teams || []) : [],
        bank: Number(pool.bank) || 0,
        winner: pool.winner || null,
        winner_team: view === 'team' ? (pool.winner_team || (pool.winner && pool.winner.id) || null) : null,
        // Статус/раунд/таймер — ТОЛЬКО своей арены: прежде сюда попадал
        // status одиночной, и командный круг «замирал» на чужом финише.
        status: pool.status || 'waiting',
        timer: Number(pool.timer) || 0,
        round_id: String(pool.round_id || ''),
        roll_data: pool.roll_data || state.roll_data || null,
        roll_duration: Number(pool.roll_duration) || 8,
    });
}

// Задача 47: плашка победителя заполняется ТОЛЬКО данными своей арены.
// info берётся из arenaWinnerInfo(view), поэтому подставить победителя другого
// режима физически нечем. Возвращает false, если показывать нечего.
function applyArenaWinnerModal(view, info) {
    if (!info) return false;
    const modal = document.getElementById('winner-modal');
    if (!modal) return false;
    // Ник — текстом (не HTML), сумма — innerHTML, потому что внутри картинка монеты.
    // В командной арене ника нет: пишем только название команды.
    const teamLabel = info.team ? (info.team === 'red' ? '🔴 ' : '🔵 ') : '';
    const teamName = String(info.name || '').toUpperCase();
    const poolNow = arenaPool(view) || {};
    // Задача 47: сервер ещё не подтвердил выплату — так и пишем, а не «0».
    const payoutPending = poolNow.payout_status === 'pending';
    const payoutFailed = poolNow.payout_status === 'failed';

    const titleEl = document.getElementById('winner-modal-title');
    const teamEl = document.getElementById('winner-modal-team');
    if (titleEl) {
        // Задача 47: в командной арене заголовок дословно «Победитель: команда
        // Красные» — раньше была отдельная строка «Победила команда» и плашка
        // ниже, из-за чего в чате/логе выглядело, будто победителя нет.
        titleEl.textContent = info.team
            ? `Победитель: команда ${teamName}`
            : 'Победитель';
    }
    if (teamEl) {
        // Задача 45: ника игрока в командной плашке НЕТ — только цвет команды.
        if (info.team) {
            teamEl.className = `winner-team-badge ${info.team === 'red' ? 'red' : 'blue'}`;
            teamEl.innerText = `${teamLabel}${teamName}`;
            teamEl.style.display = 'inline-block';
        } else {
            teamEl.className = 'winner-team-badge';
            teamEl.style.display = 'none';
            teamEl.innerText = '';
        }
    }
    const nameEl = document.getElementById('winner-modal-name');
    if (nameEl) {
        if (info.team) {
            nameEl.innerText = 'Банк поделён по ставкам';
            nameEl.style.fontSize = '15px';
            nameEl.title = 'Выигрыш делится пропорционально ставкам победившей команды';
        } else {
            nameEl.innerText = shortName(info.name, 18);
            nameEl.style.fontSize = '';
            nameEl.title = String(info.name || '');
        }
    }
    const amountEl = document.getElementById('winner-modal-amount');
    if (amountEl) {
        // Задача 45: в командной арене показываем ЧЕСТНУЮ выплату — ставка +
        // доля банка (ровно столько придёт на баланс).
        const mineBet = Number(info.bet) || 0;
        amountEl.classList.toggle('muted', info.amount <= 0);
        if (info.amount > 0) {
            amountEl.innerHTML = info.team
                ? `${coinAmount(info.amount)} <small>(ставка ${coinAmount(mineBet)} + ${coinAmount(Math.max(0, info.amount - mineBet))})</small>`
                : `+${coinAmount(info.amount)}`;
        } else if (payoutPending) {
            amountEl.innerHTML = 'Выигрыш зачисляется…';
        } else if (payoutFailed) {
            amountEl.innerHTML = 'Выплата повторяется — баланс не потерян';
        } else {
            amountEl.innerHTML = `Банк: ${coinAmount(info.bank)}`;
        }
        amountEl.style.display = '';
    }
    return true;
}

// Кто и сколько выиграл в конкретном котле — для модалки.
// Обычная арена: победитель-игрок забирает весь банк котла (или банк уходит
// боту — тогда выплаты нет). Командная: банк делят игроки победившей команды,
// поэтому «выигрыш» считаем пропорционально своей ставке.
function arenaWinnerInfo(poolView, state) {
    const pool = arenaPool(poolView);
    if (!pool.winner) return null;
    if (poolView === 'team') {
        // Задача 43: в командной арене выигрывает КОМАНДА, а не человек.
        // Раньше плашка писала ник самого крупного игрока победившей команды —
        // выглядело так, будто банк достался ему одному.
        const teamId = pool.winner_team || pool.winner.id;
        const players = (pool.players || []).filter(p => p.team === teamId);
        // Боты не делят банк: считаем долю только по живым игрокам команды.
        const humans = players.filter(p => !p.is_bot);
        const total = humans.reduce((sum, p) => sum + (Number(p.bet) || 0), 0) || 1;
        const mine = players.find(p => String(p.id) === String(user && user.id));
        // Ставка игрока и его доля банка (та же формула, что на сервере).
        const mineBet = mine ? Number(mine.bet) || 0 : 0;
        const share = mine ? Math.round((Number(pool.bank) || 0) * mineBet / total) : 0;
        return {
            key: `team:${teamId}`, team: teamId, arena: 'team',
            name: ARENA_TEAM_NAMES[teamId] || (teamId === 'blue' ? 'Синие' : 'Красные'),
            amount: share, bet: mineBet, bank: Number(pool.bank) || 0,
            members: players.filter(p => !p.is_bot).length,
        };
    }
    const winner = pool.winner;
    const mineWin = String(winner.id) === String(user && user.id);
    const isBot = Boolean(winner.is_bot) || !/^\d+$/.test(String(winner.id));
    return {
        key: `normal:${winner.id}`, team: null, arena: 'normal',
        name: winner.name || 'Игрок',
        amount: mineWin && !isBot ? (Number(pool.bank) || 0) : 0,
        bank: Number(pool.bank) || 0,
    };
}

const ARENA_TEAM_NAMES = { red: 'Красные', blue: 'Синие' };

function setArenaMode(mode) {
    const view = mode === 'team' ? 'team' : 'normal';
    if (arenaState.view === view) {
        updateArenaModeUI(view, arenaState.myTeam);
        return Promise.resolve({ mode: view, view });
    }
    arenaState.view = view;
    arenaState.mode = view;
    // Задача 33: анимации у арен ТАКИЕ ЖЕ, как всегда. Переключение не сбрасывает
    // раунд и не перезапускает движение: мяч и стрелка продолжают раунд с того
    // момента, где он идёт по серверному времени. Раньше при каждом переходе
    // мяч начинал путь заново, а стрелка второй раз «доезжала» до победителя.
    syncArenaView();
    // Догоняем статус раунда сразу, не дожидаясь следующего опроса (200 мс).
    // instant=true: раунд уже разыгран, поэтому стрелку ставим сразу на
    // победителя, а мяч — в финальную точку. Так переход не выглядит как
    // «ещё один финиш», а анимации остаются такими же, как всегда.
    const state = arenaState.last;
    if (state) replayArenaAnimation(state, true);
    return Promise.resolve({ mode: view, view });
}

// Запускает анимацию выбранной арены по последнему известному состоянию.
function replayArenaAnimation(state, instant) {
    // Задача 42: статус и round_id берём у открытой арены — её броски идут
    // независимо от соседнего котла.
    const pool = arenaPool(arenaState.view);
    const round = {
        status: pool.status || 'waiting',
        round_id: String(pool.round_id || ''),
    };
    if (round.round_id) state = Object.assign({}, state, round);
    if (!state || !state.round_id) return;
    if (state.status === 'rolling') {
        if (arenaState.view === 'team') {
            if (arenaPointer.roundId !== state.round_id) {
                arenaPointer.roundId = state.round_id;
                arenaResetPointer();
            }
            if (!arenaPointer.spinning && !arenaPointer.rafId) {
                const rollData = (arenaPool('team').roll_data) || state.roll_data || {};
                arenaBeginTeamRoll(rollData.final_x);
            }
        } else if (arenaBall.roundId !== String(state.round_id) || !arenaBall.active) {
            startArenaBall(arenaStateForView(state, 'normal'));
        }
    } else if (state.status === 'finished') {
        const finalX = state.roll_data ? Number(state.roll_data.final_x) || 0 : 0;
        if (arenaState.view === 'team') {
            if (instant && arenaPointer.landedRound !== state.round_id) {
                // Переключились уже после финиша: стрелка сразу на победителе,
                // без второго «доезда» на 3 секунды.
                arenaPointer.landedRound = state.round_id;
                arenaPointerJumpTo(finalX);
            } else if (arenaPointer.landedRound !== state.round_id) {
                arenaPointer.landedRound = state.round_id;
                arenaLandPointer(finalX, ARENA_POINTER_LAND_MS);
            } else if (instant) {
                arenaPointerJumpTo(finalX);
            }
        } else {
            // Мяч замирает в финальной точке — ровно как после обычного финиша.
            freezeArenaBall(arenaStateForView(state, 'normal'));
        }
    }
}

// Стрелка сразу на нужном угле: тот же визуальный результат, что после
// arenaLandPointer, но без повторной прокрутки (для возврата на арену).
// Задача 42: ОДНА анимация на весь бросок командной арены — 3 секунды
// раскрута (скорость растёт постепенно) и 5 секунд торможения, причём на
// последней секунде стрелка делает меньше градуса в кадр и визуально «встаёт».
// Стартуем в НАЧАЛЕ броска: финальная точка приходит с сервера сразу вместе с
// началом раунда (roll_data.final_x), поэтому не нужно ничего досчитывать после.
function arenaBeginTeamRoll(finalX, lateMs) {
    const pointer = arenaPointerNode();
    arenaPointerStopSpin();
    if (!pointer) return;
    const token = (arenaPointer.landToken || 0) + 1;
    arenaPointer.landToken = token;

    const desired = ((arenaAngleForShare(Math.max(0, Math.min(100, Number(finalX) || 0))) % 360) + 360) % 360;
    const current = ((arenaPointer.angle % 360) + 360) % 360;
    const delta = ((desired - current) % 360 + 360) % 360;
    const startAngle = arenaPointer.angle;

    // Торможение укладывается в целые обороты + доводку до победителя.
    // Разгон НЕ кратен 360°, поэтому его остаток обязательно компенсируем:
    // иначе стрелка встанет на 40–50° мимо сектора победителя.
    const spinUp = arenaSpinUpDistance();
    const downTurns = Math.max(3, Math.round(spinUp / 360));
    const spinUpRest = ((spinUp % 360) + 360) % 360;
    const need = ((delta - spinUpRest) % 360 + 360) % 360;
    const spinDown = downTurns * 360 + need;
    const total = ARENA_SPIN_UP_MS + ARENA_SPIN_DOWN_MS;

    const nowMs = arenaNowMs;
    const startedAt = nowMs();
    // Задача 70: если зашли в середине броска, отсчёт начинается «задним
    // числом» — стрелка сразу продолжает с той точки, где она была бы сейчас,
    // и не пролетает весь путь за секунду.
    const skipMs = Math.max(0, Math.min(total - 600, Number(lateMs) || 0));
    const animStart = startedAt - skipMs;
    // Запоминаем старт и длительность: по ним экран финиша понимает, сколько
    // осталось до реальной остановки стрелки, и не показывает победителя раньше.
    arenaPointer.rollStartedAt = animStart;
    arenaPointer.rollTotalMs = total;
    pointer.classList.remove('landing');
    pointer.classList.add('spinning');
    pointer.style.transition = 'none';
    arenaPointer.spinning = true;

    let finished = false;
    const finish = () => {
        if (finished) return;
        finished = true;
        if (token !== arenaPointer.landToken) return;
        arenaPointerSetAngle(startAngle + spinUp + spinDown);
        arenaPointerStopSpin();
        pointer.classList.add('landing');
        arenaPointer.landedRound = arenaPointer.roundId;
    };

    if (typeof requestAnimationFrame !== 'function') {
        // Резерв для старых WebView: один CSS-переход на всё время броска.
        pointer.style.transition = `transform ${total}ms ${ARENA_POINTER_LAND_EASING}`;
        pointer.style.transform = `rotate(${(startAngle + spinUp + spinDown).toFixed(2)}deg)`;
        arenaPointer.angle = startAngle + spinUp + spinDown;
        arenaPointer.spinning = false;
        setTimeout(finish, total + 250);
        return;
    }

    const step = () => {
        if (finished || token !== arenaPointer.landToken) return;
        const elapsed = Math.max(0, nowMs() - animStart);
        let angle;
        if (elapsed <= ARENA_SPIN_UP_MS) {
            // Разгон.
            angle = startAngle + spinUp * arenaSpinUpProgress(elapsed / ARENA_SPIN_UP_MS);
        } else {
            // Торможение: кубический ease-out — быстро в начале, почти ноль в конце.
            const u = Math.min(1, (elapsed - ARENA_SPIN_UP_MS) / ARENA_SPIN_DOWN_MS);
            const ease = 1 - Math.pow(1 - u, 3);
            angle = startAngle + spinUp + spinDown * ease;
            if (elapsed >= total) { finish(); return; }
        }
        arenaPointerSetAngle(angle);
        arenaPointer.rafId = requestAnimationFrame(step);
    };
    arenaPointer.rafId = requestAnimationFrame(step);
}

function arenaPointerJumpTo(finalX) {
    arenaPointerStopSpin();
    arenaPointer.landToken = (arenaPointer.landToken || 0) + 1;
    const pointer = arenaPointerNode();
    if (!pointer) return;
    const desired = ((arenaAngleForShare(Math.max(0, Math.min(100, Number(finalX) || 0))) % 360) + 360) % 360;
    arenaPointer.angle = desired;
    pointer.classList.add('landing');
    pointer.style.transition = 'none';
    pointer.style.transform = `rotate(${desired.toFixed(2)}deg)`;
}

// ================= ОБЫЧНАЯ АРЕНА: ПОЛЕ С МЯЧИКОМ =================
// Задача 29: арена возвращена к виду «до командной» — вертикальные сектора по
// ставке игрока, круглые аватарки внутри (размер зависит от ширины сектора) и
// мяч, который летит по траектории СЕРВЕРА (/api/hockey_roll/{id}) с плавным
// наездом камеры на концовке. Клиент ничего не решает: он только рисует.
const arenaBall = {
    roundId: null, path: null, loading: false, rafId: null,
    startedAt: 0, active: false, lastThudAt: 0, zoom: 1,
};
const arenaFieldCache = { signature: null };

// Куда смотрит камера: точка мяча остаётся на месте (transform-origin в ней),
// поэтому увеличенное поле не «уезжает» и не выходит за края арены.
const ARENA_CAMERA_ZOOM_START = 4.5;   // с этой секунды начинаем приближать
const ARENA_CAMERA_MAX_ZOOM = 1.55;    // предел наезда

function normalArenaPlayers(state) {
    const players = Array.isArray(state && state.players) ? state.players : [];
    return players.filter(player => Number(player.bet) > 0);
}

function updateArenaView(mode) {
    const isTeam = mode === 'team';
    const field = document.getElementById('hockey-field');
    const circle = document.getElementById('hockey-arena');
    if (field) field.hidden = isTeam;
    if (circle) circle.hidden = !isTeam;
    if (isTeam) {
        // Мяч останавливаем (его не видно), но roundId НЕ сбрасываем: вернёмся —
        // продолжим с той же точки траектории по серверному времени.
        stopArenaBall();
        const fieldEl = document.getElementById('hockey-field');
        if (fieldEl) fieldEl.classList.remove('rolling', 'settled');
        if (circle) circle.classList.remove('rolling', 'settled');
    } else {
        // Стрелку только останавливаем, угол и «раунд разыгран» сохраняем,
        // иначе при возврате на командную арену она крутилась бы заново.
        arenaPointerStopSpin();
        if (circle) circle.classList.remove('rolling', 'settled');
    }
}

function applyHockeyCamera(x, y, elapsedSeconds) {
    const camera = document.getElementById('hockey-camera');
    if (!camera) return;
    // Задача 70: на телефоне наезд камеры убран — scale() поверх картинки с
    // кучей теней стоил дороже всего, из-за него арена и «подлагивала».
    if (isCompactArenaViewport()) { resetArenaCamera(); return; }
    const span = Math.max(0.001, HOCKEY_ROLL_DURATION - ARENA_CAMERA_ZOOM_START);
    const progress = Math.max(0, Math.min(1, (Number(elapsedSeconds) - ARENA_CAMERA_ZOOM_START) / span));
    const eased = progress * progress * (3 - 2 * progress);   // smoothstep
    const scale = 1 + (ARENA_CAMERA_MAX_ZOOM - 1) * eased;
    arenaBall.zoom = scale;
    camera.style.transformOrigin = `${Math.max(0, Math.min(100, Number(x) || 0))}% ${Math.max(0, Math.min(100, Number(y) || 0))}%`;
    camera.style.transform = `scale(${scale.toFixed(4)})`;
}

// Телефонный режим арены: узкий экран (или экономия движения в системе).
function isCompactArenaViewport() {
    try {
        if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return true;
        return window.innerWidth <= 719;
    } catch (e) {
        return true;
    }
}

function resetArenaCamera() {
    const camera = document.getElementById('hockey-camera');
    if (!camera) return;
    arenaBall.zoom = 1;
    camera.style.transform = 'scale(1)';
    camera.style.transformOrigin = '50% 50%';
}

function placeArenaBall(x, y) {
    const ball = document.getElementById('hockey-ball');
    if (!ball) return;
    ball.hidden = false;
    ball.style.left = `${Math.max(0, Math.min(100, Number(x) || 0)).toFixed(2)}%`;
    ball.style.top = `${Math.max(0, Math.min(100, Number(y) || 0)).toFixed(2)}%`;
}

// Полосы: аватарка по центру сектора, без подписей — как было до командной
// арены. Перерисовываем только когда действительно изменились игроки/ставки,
// иначе анимации стартовали заново и «дёргались».
function renderNormalArena(state) {
    const scene = document.getElementById('hockey-scene');
    if (!scene) return;
    const players = normalArenaPlayers(state);
    const finished = Boolean(state && state.status === 'finished');
    const winnerId = state && state.winner ? String(state.winner.id) : null;
    const signature = arenaSignature(players, `${finished}:${winnerId || ''}`);
    if (signature === arenaFieldCache.signature) return;
    arenaFieldCache.signature = signature;

    // Убираем прежние сектора, оставляя угловую подсветку и мяч.
    scene.querySelectorAll('.hockey-sector').forEach(el => el.remove());
    const emptyEl = scene.querySelector('.arena-empty');
    if (!players.length) {
        if (!emptyEl) {
            const empty = document.createElement('div');
            empty.className = 'arena-empty';
            empty.textContent = 'Ждём ставок…';
            scene.appendChild(empty);
        }
        return;
    }
    if (emptyEl) emptyEl.remove();

    const arena = document.getElementById('hockey-field');
    const arenaWidth = (arena && arena.clientWidth) || 350;
    const total = players.reduce((sum, player) => sum + Math.max(0, Number(player.bet) || 0), 0) || 1;
    let usedWidth = 0;
    players.forEach((player, index) => {
        // Последний сектор добирает остаток: сумма ширин всегда ровно 100%.
        let width = index === players.length - 1
            ? 100 - usedWidth
            : Math.max(0, Number(player.bet) || 0) / total * 100;
        if (index === players.length - 1) width = Math.max(0.5, width);
        else usedWidth += width;

        const sector = document.createElement('div');
        sector.className = 'hockey-sector';
        sector.style.width = `${width.toFixed(4)}%`;
        sector.style.background = player.color || '#5856d6';
        sector.dataset.playerId = String(player.id);
        if (finished && winnerId && String(player.id) === winnerId) sector.classList.add('win');

        const wrap = document.createElement('div');
        wrap.className = 'player-shape-wrapper';
        const shape = document.createElement('div');
        shape.className = 'player-shape';
        const size = Math.max(25, Math.min(60, (width / 100) * arenaWidth * 0.7));
        shape.style.width = `${size.toFixed(0)}px`;
        shape.style.height = `${size.toFixed(0)}px`;
        const name = String(player.name || '?');
        if (player.avatar && String(player.avatar).startsWith('http')) {
            shape.innerHTML = `<img src="${escapeHtml(player.avatar)}" alt="">`;
            shape.style.background = 'transparent';
        } else {
            shape.style.backgroundColor = player.color || '#5856d6';
            shape.innerHTML = `<span class="player-shape-letter" style="font-size:${(size * 0.5).toFixed(0)}px">`
                + `${escapeHtml(name.slice(0, 1).toUpperCase())}</span>`;
        }
        wrap.appendChild(shape);
        sector.appendChild(wrap);
        scene.appendChild(sector);
    });
    const ball = document.getElementById('hockey-ball');
    if (ball) scene.appendChild(ball);   // мяч всегда поверх секторов
}

function stopArenaBall() {
    if (arenaBall.rafId) cancelAnimationFrame(arenaBall.rafId);
    arenaBall.rafId = null;
    arenaBall.active = false;
}

function hideArenaBall() {
    stopArenaBall();
    const ball = document.getElementById('hockey-ball');
    if (ball) { ball.hidden = true; ball.style.left = '50%'; ball.style.top = '50%'; }
    const field = document.getElementById('hockey-field');
    if (field) field.classList.remove('rolling', 'settled');
    resetArenaCamera();
    arenaBall.roundId = null;
    arenaBall.path = null;
    arenaBall.loading = false;
}

// Траектория раунда: один запрос на раунд, ответ кэшируем.
const arenaPathCache = { roundId: null, path: null, pending: null };

async function loadArenaRollPath(roundId) {
    if (arenaPathCache.roundId === roundId && arenaPathCache.path) return arenaPathCache.path;
    if (arenaPathCache.roundId === roundId && arenaPathCache.pending) return arenaPathCache.pending;
    arenaPathCache.roundId = roundId;
    arenaPathCache.path = null;
    const pending = (async () => {
        const res = await fetch(`${API_URL}/api/hockey_roll/${encodeURIComponent(roundId)}?initData=${encodeURIComponent(tg.initData || '')}`);
        if (!res.ok) throw new Error('Не удалось получить траекторию мяча');
        const data = await res.json();
        if (!data || !Array.isArray(data.path) || data.path.length < 2) throw new Error('Нет траектории');
        const path = data.path.map(point => [Number(point[0]), Number(point[1])]);
        if (arenaPathCache.roundId === roundId) arenaPathCache.path = path;
        return path;
    })();
    arenaPathCache.pending = pending.catch(error => {
        if (arenaPathCache.roundId === roundId) arenaPathCache.pending = null;
        throw error;
    });
    return arenaPathCache.pending;
}

// Сколько секунд прошло с начала броска по СЕРВЕРНЫМ часам: мяч окажется ровно
// там, где он сейчас, даже если экран открыли на середине раунда.
function arenaElapsedSeconds() {
    return arenaElapsedSecondsFor(arenaBall.startedAt);
}

function arenaElapsedSecondsFor(startedAtSeconds) {
    const startedAt = Number(startedAtSeconds) || 0;
    if (!startedAt) return 0;
    const serverNowMs = performance.now() + hockeyServerOffsetMs;
    return Math.max(0, (serverNowMs - startedAt * 1000) / 1000);
}

// Глухой удар, когда мяч меняет направление (отскок от борта).
function playArenaBounceSound(points, frame) {
    if (!points || frame < 2 || !points[frame] || !points[frame - 1] || !points[frame - 2]) return;
    const dx1 = points[frame][0] - points[frame - 1][0];
    const dx2 = points[frame - 1][0] - points[frame - 2][0];
    const dy1 = points[frame][1] - points[frame - 1][1];
    const dy2 = points[frame - 1][1] - points[frame - 2][1];
    const bounced = (Math.abs(dx1) > 0.4 && Math.abs(dx2) > 0.4 && Math.sign(dx1) !== Math.sign(dx2))
        || (Math.abs(dy1) > 0.4 && Math.abs(dy2) > 0.4 && Math.sign(dy1) !== Math.sign(dy2));
    if (!bounced) return;
    const now = Date.now();
    if (now - arenaBall.lastThudAt < 140) return;   // не строчим звуком на каждом кадре
    arenaBall.lastThudAt = now;
    playSound('thud');
}

function startArenaBall(state) {
    const roundId = state && state.round_id ? String(state.round_id) : null;
    if (!roundId) return;
    stopArenaBall();
    const field = document.getElementById('hockey-field');
    if (field) { field.classList.add('rolling'); field.classList.remove('settled'); }
    resetArenaCamera();
    arenaBall.roundId = roundId;
    arenaBall.path = null;
    arenaBall.loading = true;
    arenaBall.active = true;
    arenaBall.startedAt = Number(state && state.rolling_started_at) || (Date.now() / 1000);
    placeArenaBall(50, 50);
    // Задача 70: если мы зашли в середине броска (или телефон проснулся, или
    // сеть подвисла), траектория приезжает уже после финиша раунда. Раньше в
    // этот момент мяч просто «телепортировался» на победителя. Теперь такая
    // ситуация видна сразу: как только известна финальная точка — ставим мяч
    // туда, без промежуточного кадра.
    const finishNow = () => {
        if (!arenaBall.active || arenaBall.roundId !== roundId) return;
        arenaBall.active = false;
        arenaBall.rafId = null;
        const roll = (state && state.roll_data) || null;
        if (roll && Number.isFinite(Number(roll.final_x))) {
            freezeArenaBall(Object.assign({}, state, { round_id: roundId, roll_data: roll }));
        }
    };
    if (arenaElapsedSecondsFor(arenaBall.startedAt) >= HOCKEY_ROLL_DURATION) { finishNow(); return; }
    loadArenaRollPath(roundId).then(path => {
        if (!arenaBall.active || arenaBall.roundId !== roundId) return;
        arenaBall.path = path;
        arenaBall.loading = false;
        // Пока грузилась траектория, раунд успел закончиться — не начинаем
        // анимацию «с начала», иначе мяч прыгает на последнем кадре.
        if (arenaElapsedSeconds() >= HOCKEY_ROLL_DURATION) { finishNow(); return; }
        animateArenaBall();
    }).catch(() => { arenaBall.loading = false; });
}

function animateArenaBall() {
    const roundId = arenaBall.roundId;
    const step = () => {
        if (!arenaBall.active || arenaBall.roundId !== roundId) return;
        const elapsed = arenaElapsedSeconds();
        const points = arenaBall.path;
        if (points && points.length > 1) {
            // Шаг симуляции 0.05 с — ровно как отдаёт сервер, поэтому мяч
            // движется с той же скоростью, с какой считалась траектория.
            const framePosition = Math.max(0, Math.min(points.length - 1, elapsed / HOCKEY_SIMULATION_STEP));
            const frame = Math.floor(framePosition);
            const next = Math.min(points.length - 1, frame + 1);
            const fraction = framePosition - frame;
            const bx = points[frame][0] + (points[next][0] - points[frame][0]) * fraction;
            const by = points[frame][1] + (points[next][1] - points[frame][1]) * fraction;
            placeArenaBall(bx, by);
            applyHockeyCamera(bx, by, elapsed);
            playArenaBounceSound(points, frame);
        }
        if (elapsed < HOCKEY_ROLL_DURATION) {
            arenaBall.rafId = requestAnimationFrame(step);
        } else {
            arenaBall.active = false;
            arenaBall.rafId = null;
        }
    };
    arenaBall.rafId = requestAnimationFrame(step);
}

// Финальная точка мяча (по серверу): мяч замирает на выигрышном секторе,
// камера остаётся на нём, победная полоса подсвечивается.
function freezeArenaBall(state) {
    stopArenaBall();
    const field = document.getElementById('hockey-field');
    if (field) { field.classList.remove('rolling'); field.classList.add('settled'); }
    const roll = state && state.roll_data;
    if (!roll) return;
    const x = Number(roll.final_x);
    const y = Number(roll.final_y);
    if (!Number.isFinite(x)) return;
    const fy = Number.isFinite(y) ? y : 50;
    placeArenaBall(x, fy);
    applyHockeyCamera(x, fy, HOCKEY_ROLL_DURATION);
}

async function pollHockeyState() {
    if (!document.getElementById('screen-hockey').classList.contains('active')) return;
    // Задача 70: свёрнутая аппка не должна тянуть ответы сервера — на телефоне
    // это и батарея, и «лаги» при возврате (пока догоняешь 20 запросов).
    if (document.visibilityState === 'hidden') return;
    // Опрос не должен наслаиваться: если Render отвечает медленно, следующий
    // тик ждёт, а не плодит параллельные запросы (они и тормозили арену).
    if (arenaPollInFlight) return;
    arenaPollInFlight = true;
    try {
        const requestStartedAt = performance.now();
        // Задача 57: состояние арены закрыто админами, поэтому передаём initData —
        // на сервере техработы отдают его только тем, кому можно играть.
        const res = await fetch(`${API_URL}/api/hockey_state?initData=${encodeURIComponent(tg.initData || '')}`);
        const requestFinishedAt = performance.now();
        // 5xx/429 от Render — не повод падать: держим последнее известное
        // состояние. Раньше разбор ответа с ошибкой бросал TypeError каждые
        // 200 мс, и экран Арены замирал.
        if (!res.ok) return;
        const state = await res.json().catch(() => null);
        if (!state || !state.status) return;
        if (Number.isFinite(Number(state.server_time))) {
            const estimatedOffset = Number(state.server_time) * 1000 - (requestStartedAt + requestFinishedAt) / 2;
            hockeyServerOffsetMs = hockeyClockSynced ? hockeyServerOffsetMs * 0.8 + estimatedOffset * 0.2 : estimatedOffset;
            hockeyClockSynced = true;
        }
        const statusEl = document.getElementById('hockey-status');
        const bankEl = document.getElementById('hockey-bank');
        const arena = document.getElementById('hockey-arena');

        // Обе арены приходят одним ответом; какую рисуем — решает игрок.
        // Задача 42: у каждой арены СВОЙ раунд, поэтому статус/таймер/round_id
        // берём из пула выбранной арены, а не из «верхних» полей ответа.
        arenaState.last = state;
        arenaState.arenas = arenaPoolsFromState(state);
        syncArenaView();
        const isTeamMode = arenaState.view === 'team';
        const activePool = arenaPool(arenaState.view);
        const round = {
            status: activePool.status || 'waiting',
            timer: Number(activePool.timer) || 0,
            round_id: String(activePool.round_id || ''),
        };
        arenaState.status = round.status;

        if (round.status === 'waiting') {
            statusEl.innerText = `Ставки: ${round.timer} сек`; statusEl.style.color = '#ffcc00';
            document.getElementById('hockey-controls').style.display = 'flex';
            // Задача 47: ключ «кому уже показали» — свой на каждую арену.
            // Общий ключ не давал показать победителя командной после того,
            // как плашку одиночной уже увидели (и наоборот).
            hasShownWinner = false;
            shownWinnerKey = null;
            hideArenaBall();
            // Новый раунд — стрелка в исходное положение. Проверяем по
            // round_id, чтобы не сбрасывать её на каждом опросе.
            if (arenaPointer.roundId !== round.round_id) {
                arenaPointer.roundId = round.round_id;
                arenaResetPointer();
            }
            document.getElementById('winner-modal').classList.remove('active');
            document.getElementById('hockey-btn').disabled = false;
        } else if (round.status === 'rolling') {
            statusEl.innerText = 'Идёт бросок...'; statusEl.style.color = '#007aff';
            document.getElementById('hockey-controls').style.display = 'none';
            if (isTeamMode) {
                // Командная арена: 2 с разгон + 3.5 с торможение (как апгрейд).
                // Запускаем ОДИН раз на раунд, в начале броска; финальную точку
                // сервер отдаёт сразу, поэтому анимация нигде не «догоняет».
                if (arenaPointer.roundId !== round.round_id) {
                    arenaPointer.roundId = round.round_id;
                    arenaResetPointer();
                    arenaPointer.roundId = round.round_id;
                    arenaRollClock = { roundId: round.round_id, startedAtMs: 0, totalMs: 0 };
                }
                const teamRoll = activePool.roll_data || state.roll_data || {};
                const rollStartedAtMs = Number(activePool.rolling_started_at
                    || state.rolling_started_at || 0) * 1000;
                // Задача 70: «шарик сразу становится на победителя». Причина —
                // поздний вход: игрок открыл арену/вернулся в аппку в середине
                // броска, анимация считала elapsed от своего старта и через
                // секунду прыгала в конец. Время броска берём у сервера.
                if (rollStartedAtMs > 0) {
                    const serverNowMs = arenaNowMs() + hockeyServerOffsetMs;
                    const rollLeft = Math.max(0, (rollStartedAtMs + ARENA_TEAM_ROLL_MS) - serverNowMs);
                    const lateMs = Math.max(0, serverNowMs - rollStartedAtMs);
                    arenaRollClock = { roundId: round.round_id, startedAtMs: rollStartedAtMs, totalMs: ARENA_TEAM_ROLL_MS };
                    if (rollLeft <= 1200) {
                        // Осталось меньше секунды: нормальный финиш не покажешь,
                        // поэтому сразу ставим стрелку на победителя и помечаем
                        // раунд завершённым — без рывка и без «прыжка».
                        arenaPointerJumpTo(teamRoll.final_x);
                        arenaPointer.landedRound = round.round_id;
                        arenaPointer.rollStartedAt = arenaNowMs() - lateMs;
                        arenaPointer.rollTotalMs = ARENA_TEAM_ROLL_MS;
                    } else if (arenaPointer.landedRound !== round.round_id
                            && !arenaPointer.spinning && !arenaPointer.rafId) {
                        arenaBeginTeamRoll(teamRoll.final_x, lateMs);
                    }
                } else if (arenaPointer.landedRound !== round.round_id
                        && !arenaPointer.spinning && !arenaPointer.rafId) {
                    arenaBeginTeamRoll(teamRoll.final_x, 0);
                }
            } else if (arenaBall.roundId !== String(round.round_id) || !arenaBall.active) {
                // Обычная арена: один раз на раунд берём траекторию и запускаем мяч.
                // !active — вернулись с другой арены: продолжаем с текущей точки.
                startArenaBall(arenaStateForView(state, 'normal'));
            }
        } else if (round.status === 'finished') {
            statusEl.innerText = 'Раунд окончен!'; statusEl.style.color = '#ff3b30';
            // Задача 34: победителя показываем не сразу, а когда анимация
            // доехала до конца. Раньше модалка всплывала через 1 с — стрелка
            // ещё крутилась, и финиш выглядел резким обрывом.
            let winnerDelayMs = ARENA_WINNER_DELAY_MS;
            if (isTeamMode) {
                // Задача 42: анимация началась в начале броска и длится 8 секунд
                // (3 разгон + 5 торможение). Победителя показываем не раньше
                // фиксации стрелки — досчитываем остаток, если сервер успел
                // закрыть раунд, а мы подключились позже.
                const rollData = activePool.roll_data || state.roll_data || {};
                const finalX = Number(rollData.final_x) || 0;
                if (arenaPointer.landedRound !== round.round_id) {
                    // Задача 70: раунд уже закрыт, а мы видели только начало —
                    // не крутим полные 5.5 с «в пустоту», досчитываем от
                    // серверного времени старта броска.
                    const rollStartedMs = Number(activePool.rolling_started_at
                        || state.rolling_started_at || 0) * 1000;
                    const serverNowMs = arenaNowMs() + hockeyServerOffsetMs;
                    const lateMs = rollStartedMs > 0
                        ? Math.max(0, serverNowMs - rollStartedMs) : 0;
                    if (!arenaPointer.spinning && !arenaPointer.rafId) {
                        arenaPointer.roundId = round.round_id;
                        if (rollStartedMs > 0 && lateMs >= ARENA_TEAM_ROLL_MS - 1000) {
                            arenaPointerJumpTo(finalX);
                            arenaPointer.landedRound = round.round_id;
                            arenaPointer.rollStartedAt = arenaNowMs();
                            arenaPointer.rollTotalMs = 0;
                        } else {
                            arenaBeginTeamRoll(finalX, lateMs);
                        }
                    }
                    // Сообщим победителя, когда стрелка реально встанет: берём
                    // остаток анимации от её старта, а не фиксированные 5 секунд
                    // (иначе модалка опаздывала на весь разгон).
                    arenaPointer.pendingWinnerRound = round.round_id;
                    const rollLeft = (arenaPointer.rollStartedAt && arenaPointer.rollTotalMs)
                        ? Math.max(0, Number(arenaPointer.rollTotalMs)
                            - (arenaNowMs() - Number(arenaPointer.rollStartedAt)))
                        : 0;
                    winnerDelayMs = rollLeft + ARENA_WINNER_AFTER_LAND_MS;
                } else {
                    winnerDelayMs = ARENA_WINNER_DELAY_MS;
                }
            } else {
                freezeArenaBall(arenaStateForView(state, 'normal'));
            }
            // Задача 47: режимы независимы — победитель показывается ТОЛЬКО
            // для той арены, которую смотрит игрок. Раньше при пустой своей
            // арене подставлялся победитель другой, и в командной арене
            // всплывал ник игрока из одиночной (и наоборот).
            const primary = arenaState.view === 'team' ? 'team' : 'normal';
            const winnerKey = `${primary}:${round.round_id}:${arenaPool(primary).winner_team || ''}`;
            if (arenaPool(primary).winner && shownWinnerKey !== winnerKey) {
                shownWinnerKey = winnerKey;
                hasShownWinner = true;
                const info = arenaWinnerInfo(primary, state);
                setTimeout(() => {
                    const modal = document.getElementById('winner-modal');
                    if (!applyArenaWinnerModal(primary, info)) return;
                    modal.classList.add('active');

                    // The database is the only balance source; never optimistically add the pot here.
                    loadUserData();
                    if (info.amount > 0) { playSound('win'); } else { playSound('arena_lose'); }
                }, winnerDelayMs);
            }
        }
        // Банк и поле переключились выше, в syncArenaView() — рисуем ровно ту
        // арену, которая открыта, из её собственного котла.
        if (arena) {
            arena.classList.toggle('team-mode', isTeamMode);
            // Анимации командной арены: «бегущее» кольцо во время броска и
            // золотая пульсация круга после него.
            arena.classList.toggle('rolling', isTeamMode && round.status === 'rolling');
            arena.classList.toggle('settled', isTeamMode && round.status === 'finished');
        }
    } catch (e) { console.error(e); }
    finally { arenaPollInFlight = false; }
}

function startHockeyPolling() {
    if (hockeyInterval) clearInterval(hockeyInterval);
    pollHockeyState();
    // Задача 70: было 200 мс — телефон грелся и арена «подлагивала». Анимация
    // всё равно рисуется на клиенте каждый кадр, а с сервера нужны только
    // статус, таймер и победитель, поэтому 500 мс достаточно.
    hockeyInterval = setInterval(pollHockeyState, 500);
}
function stopHockeyPolling() {
    if (hockeyInterval) clearInterval(hockeyInterval);
    hockeyInterval = null;
    // Стрелку и мяч останавливаем вместе с опросом: иначе они продолжали бы
    // двигаться на уже скрытом экране и жгли батарею.
    arenaPointer.spinning = false;
    if (arenaPointer.rafId) cancelAnimationFrame(arenaPointer.rafId);
    arenaPointer.rafId = null;
    stopArenaBall();
}
function closeWinnerModal() { document.getElementById('winner-modal').classList.remove('active'); }

// ================= РАКЕТА =================
async function startCrashPolling() {
    if (crashInterval) clearInterval(crashInterval);
    await pollCrashState();
    crashInterval = setInterval(pollCrashState, 400);
}
function stopCrashPolling() {
    if (crashInterval) clearInterval(crashInterval);
    crashInterval = null;
    if (animFrameId) cancelAnimationFrame(animFrameId);
    isAnimatingFlight = false;
}

async function crashAction() {
    if (isProcessing) return;
    isProcessing = true;
    try {
        if (!userDataLoaded) {
            await loadUserData();
            if (!userDataLoaded) { isProcessing = false; return tg.showAlert("Не удалось загрузить баланс. Проверьте соединение и попробуйте снова."); }
        }
        await pollCrashState();
        const s = currentCrashData;
        if (!s || Date.now() - crashStateUpdatedAt > 5000) {
            isProcessing = false;
            return tg.showAlert("Загружаем состояние игры. Попробуйте ещё раз через секунду.");
        }

        if (s.my_status === 'none' || (s.my_status === 'bet' && s.status === 'waiting')) {
            const bet = getBet('crash');
            // Задача 58: пустое поле больше не молчит нулём — объясняем игроку.
            if (bet < 1) { isProcessing = false; return tg.showAlert("Минимальная ставка 1 коин"); }
            if (balance < bet) { isProcessing = false; return tg.showAlert("Недостаточно коинов!"); }
            if (!isAdminId(user && user.id) && bet > 10000) { isProcessing = false; return tg.showAlert("Максимальная ставка 10 000 коинов"); }

            const res = await fetch(`${API_URL}/api/crash_bet`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: tg.initData, amount: bet })
            });
            if (!res.ok) {
                const e = await res.json().catch(() => ({}));
                if (res.status >= 500) { await loadUserData(); await pollCrashState(); }
                isProcessing = false;
                return tg.showAlert(e.detail || "Ошибка");
            }
            const data = await res.json();
            balance = data.new_balance; balanceKnown = true; updateUI();
            playSound('click');
            await pollCrashState();
        } else if (s.my_status === 'bet' && s.status === 'flying') {
            const res = await fetch(`${API_URL}/api/crash_cashout`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: tg.initData })
            });
            const data = await res.json().catch(()=>({}));
            if (!res.ok) {
                if (res.status >= 500) { await loadUserData(); await pollCrashState(); }
                isProcessing = false;
                return tg.showAlert(data.detail || "Не успели забрать 😢");
            }
            balance = data.new_balance; balanceKnown = true; updateUI();
            showWin(`+${coinAmount(data.win_amount)}`);
            playSound('win');
            await pollCrashState();
        }
    } catch(e) {
        console.error("Ошибка действия в Краше", e);
        await loadUserData();
        await pollCrashState();
        tg.showAlert("Не удалось подтвердить действие. Баланс и состояние игры обновлены.");
    }
    isProcessing = false;
}

// ИДЕАЛЬНО ПЛАВНАЯ АНИМАЦИЯ
function animateFlight() {
    if (!currentCrashData || currentCrashData.status !== 'flying') {
        isAnimatingFlight = false;
        return;
    }

    const el = (id) => document.getElementById(id);
    const multEl = el('crash-multiplier');
    const rocketEl = el('crash-rocket');
    const field = el('crash-field');

    if (!isAnimatingFlight && currentCrashData.status_start) {
        flightStartTime = currentCrashData.status_start;
        isAnimatingFlight = true;
    }

    const elapsed = (Date.now() / 1000) - flightStartTime;

    let smoothMult = 1.0;
    if (elapsed <= 2.0) {
        const t = elapsed / 2.0;
        smoothMult = 1.0 + (t ** 2) * 0.2;
    } else {
        smoothMult = 1.2 + (elapsed - 2.0) * 0.15 + Math.pow(elapsed - 2.0, 1.2) * 0.05;
    }

    if (smoothMult > currentCrashData.multiplier) {
        smoothMult = currentCrashData.multiplier;
    }

    if (multEl) {
        multEl.innerText = `x${smoothMult.toFixed(2)}`;
        multEl.style.color = smoothMult > 2 ? '#ffcc00' : '#fff';
    }

    const btn = el('crash-btn');
    if (btn && currentCrashData.my_status === 'bet') {
        btn.innerText = `Забрать (x${smoothMult.toFixed(2)})`;
        if (smoothMult > 2) btn.classList.add('pulse'); else btn.classList.remove('pulse');
    }

    if (field && rocketEl) {
        const w = field.offsetWidth, h = field.offsetHeight;
        const t = Math.min((smoothMult - 1) / 15, 1);
        const x = 40 + Math.pow(t, 1.2) * (w - 100);
        const y = 40 + Math.pow(t, 1.2) * (h - 100);
        rocketEl.style.transition = 'none';
        rocketEl.style.left = x + 'px';
        rocketEl.style.bottom = y + 'px';
    }

    if (isAnimatingFlight) {
        animFrameId = requestAnimationFrame(animateFlight);
    }
}

async function pollCrashState() {
    if (crashPollPromise) return crashPollPromise;
    crashPollPromise = pollCrashStateOnce();
    try { return await crashPollPromise; }
    finally { crashPollPromise = null; }
}

async function pollCrashStateOnce() {
    if (!document.getElementById('screen-crash').classList.contains('active')) return;
    try {
        const url = `${API_URL}/api/crash_state?initData=${encodeURIComponent(tg.initData || '')}`;
        const res = await fetch(url);
        const state = await res.json().catch(() => ({}));
        // Тихий выход: кратковременные 5xx/429 от Render — это не ошибка игрока,
        // а console.error каждые 400 мс забивал консоль и мешал искать настоящие баги.
        if (!res.ok || !state.status) {
            crashPollFailures = (crashPollFailures || 0) + 1;
            if (crashPollFailures === 1 || crashPollFailures % 25 === 0) {
                console.warn('Краш: состояние недоступно', state.detail || res.status, 'подряд:', crashPollFailures);
            }
            return;
        }
        crashPollFailures = 0;
        currentCrashData = state;
        crashStateUpdatedAt = Date.now();

        const statusEl = document.getElementById('crash-status');
        const multEl = document.getElementById('crash-multiplier');
        const rocketEl = document.getElementById('crash-rocket');
        const btn = document.getElementById('crash-btn');
        const betContainer = document.getElementById('crash-bet-container');
        const historyEl = document.getElementById('crash-history-bar');

        if (state.status === 'waiting') {
            if (isAnimatingFlight) { cancelAnimationFrame(animFrameId); isAnimatingFlight = false; }

            statusEl.innerText = `Ставки: ${state.timer} сек`; statusEl.style.color = '#ffcc00';
            multEl.innerText = 'x1.00'; multEl.style.color = '#fff';
            betContainer.style.display = 'block';
            rocketEl.style.left = '40px'; rocketEl.style.bottom = '40px'; rocketEl.style.transform = 'rotate(45deg)'; rocketEl.style.opacity = '1';
            document.getElementById('crash-flame').style.display = 'block'; document.getElementById('crash-explosion').style.display = 'none';

            if (state.my_status === 'none') { btn.disabled = false; btn.innerText = 'Поставить ставку'; btn.style.background = '#007aff'; }
            else if (state.my_status === 'bet') { btn.disabled = false; btn.innerHTML = `Добавить (+${coinAmount(getBet('crash'))})`; btn.style.background = '#007aff'; }

        } else if (state.status === 'ready') {
            if (isAnimatingFlight) { cancelAnimationFrame(animFrameId); isAnimatingFlight = false; }

            statusEl.innerText = `Готовность: ${state.timer} сек`; statusEl.style.color = '#ff9500';
            multEl.innerText = 'x1.00'; multEl.style.color = '#fff';
            betContainer.style.display = 'none';
            rocketEl.style.left = '40px'; rocketEl.style.bottom = '40px';
            btn.disabled = true; btn.innerText = `Готовность ${state.timer}...`; btn.style.background = '#555';

        } else if (state.status === 'flying') {
            statusEl.innerText = 'Идёт полет...'; statusEl.style.color = '#34c759';
            betContainer.style.display = 'none';

            if (!isAnimatingFlight) {
                isAnimatingFlight = true;
                flightStartTime = state.status_start;
                animateFlight();
            }

            if (state.my_status === 'bet') {
                btn.disabled = false; btn.style.background = '#34c759';
            } else if (state.my_status === 'cashed_out') {
                btn.disabled = true; btn.innerText = `Забрано ✅ (x${state.my_cashout_mult.toFixed(2)})`; btn.style.background = '#555'; btn.classList.remove('pulse');
            } else {
                btn.disabled = true; btn.innerText = 'Ожидание раунда...'; btn.style.background = '#555';
            }

        } else if (state.status === 'crashed') {
            if (isAnimatingFlight) { cancelAnimationFrame(animFrameId); isAnimatingFlight = false; }

            statusEl.innerText = 'КРАШ!'; statusEl.style.color = '#ff3b30';
            multEl.innerText = `x${(state.crash_point || 0).toFixed(2)}`; multEl.style.color = '#ff3b30';
            betContainer.style.display = 'none';
            rocketEl.style.opacity = '0'; document.getElementById('crash-flame').style.display = 'none';
            const exp = document.getElementById('crash-explosion');
            exp.style.display = 'block'; exp.style.left = rocketEl.style.left; exp.style.bottom = rocketEl.style.bottom;

            btn.classList.remove('pulse');
            btn.disabled = false; btn.innerText = 'Поставить ставку'; btn.style.background = '#007aff';

            if (lastCrashStatus !== 'crashed') {
                playSound('explosion');
                setTimeout(() => { exp.style.display = 'none'; }, 600);
                setTimeout(async () => { await loadUserData(); }, 1000);
            }
        }

        if (historyEl) {
            historyEl.innerHTML = '';
            const firstDiv = document.createElement('div');
            firstDiv.className = 'crash-hist-item';
            firstDiv.style.background = '#34c759';
            firstDiv.style.color = '#000';
            firstDiv.style.fontWeight = '900';
            if (state.status === 'waiting') firstDiv.innerText = 'Ожидание';
            else if (state.status === 'ready') firstDiv.innerText = 'Готовность';
            else if (state.status === 'flying') firstDiv.innerText = `x${state.multiplier.toFixed(2)}`;
            else if (state.status === 'crashed') { firstDiv.innerText = `x${state.crash_point.toFixed(2)}`; firstDiv.style.background = '#ff3b30'; firstDiv.style.color = '#fff'; }
            historyEl.appendChild(firstDiv);

            (state.history || []).forEach(h => {
                const div = document.createElement('div');
                div.className = 'crash-hist-item';
                const cp = typeof h.crash_point === 'number' ? h.crash_point : 0;
                div.innerText = `x${Number(cp).toFixed(2)}`;
                if (cp < 2) div.style.color = '#ff3b30'; else if (cp < 5) div.style.color = '#ffcc00'; else div.style.color = '#34c759';
                historyEl.appendChild(div);
            });
        }

        const pl = document.getElementById('crash-participants');
        if (pl) {
            pl.innerHTML = '';
            const myUid = user && user.id ? String(user.id) : '';
            (state.players || []).forEach(p => {
                const d = document.createElement('div'); d.className = 'crash-part-item';
                const isMe = myUid && String(p.id) === myUid;
                const lost = state.status === 'crashed' && p.cashout_mult === 0;
                const cashed = p.cashout_mult > 0;
                let right;
                if (lost) right = `<span style="color:#ff3b30;font-weight:800">💀 -${coinAmount(p.bet)}</span>`;
                else if (cashed) { const win = Math.floor(p.bet * p.cashout_mult); right = `<span style="color:#34c759;font-weight:800">x${Number(p.cashout_mult).toFixed(2)} → +${coinAmount(win)}</span>`; }
                else right = `<span>${coinAmount(p.bet)}</span>`;
                d.innerHTML = `<span style="${isMe ? 'color:#007aff;font-weight:800' : ''}" title="${escapeHtml(p.name)}">${escapeHtml(shortName(p.name))}${isMe ? ' (вы)' : ''}</span>${right}`;
                if (lost) d.style.background = 'rgba(255,59,48,0.15)'; else if (cashed) d.style.background = 'rgba(52,199,89,0.10)';
                if (isMe) d.style.border = '1px solid #007aff';
                pl.appendChild(d);
            });
        }
        lastCrashStatus = state.status;
    } catch(e) { console.error(e); }
}

// ================= ЗАСТАВКА =================
// Экран загрузки живёт минимум 1 секунду и максимум 2: даже если данные пришли
// мгновенно, анимация успевает проиграться, а если сеть висит — экран сам уходит.
const LOADER_MIN_MS = 1000, LOADER_MAX_MS = 2000;
let loaderStartedAt = Date.now(), loaderHidden = false, loaderMaxTimer = null;

function removeLoaderNode(loader) {
    if (loader && loader.parentNode) loader.parentNode.removeChild(loader);
}

function hideLoader(force) {
    if (loaderHidden) return;
    const elapsed = Date.now() - loaderStartedAt;
    if (!force && elapsed < LOADER_MIN_MS) {
        setTimeout(() => hideLoader(true), LOADER_MIN_MS - elapsed);
        return;
    }
    loaderHidden = true;
    if (loaderMaxTimer) { clearTimeout(loaderMaxTimer); loaderMaxTimer = null; }
    const loader = document.getElementById('app-loader');
    if (!loader) return;
    loader.classList.add('hidden');
    setTimeout(() => removeLoaderNode(loader), 600);
}

// ================= ПРОГРЕВ СЕРВЕРА =================
// Render на бесплатном тарифе усыпляет сервис через 15 минут без входящих
// запросов. Тогда игрок вместо игры видит экран Render «SERVICE WAKING UP» —
// это его страница, наше приложение в этот момент ещё не загружено.
// Пока аппка открыта, раз в 4 минуты дёргаем дешёвый /api/ping: так сервис не
// успевает уснуть между экранами, и следующий переход не ждёт минуту.
const API_PING_INTERVAL = 240000;   // 4 минуты (< 15 минут сна у Render)
let apiPingTimer = null;

function pingServer() {
    // Никаких await и обработки ошибок: это фоновая «поддержка тепла».
    try {
        fetch(`${API_URL}/api/ping`, { cache: 'no-store' }).catch(() => {});
    } catch (e) {}
}

function startApiHeartbeat() {
    if (apiPingTimer) return;
    apiPingTimer = setInterval(() => {
        if (document.visibilityState === 'visible') pingServer();
    }, API_PING_INTERVAL);
    // Вернулись в аппку после паузы — сразу будим сервер, не ждём таймера.
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') pingServer();
    });
}

function startLoaderClock() {
    loaderStartedAt = Date.now();
    if (loaderMaxTimer) clearTimeout(loaderMaxTimer);
    loaderMaxTimer = setTimeout(() => hideLoader(true), LOADER_MAX_MS);
}

// ================= СТАРТ =================
document.addEventListener('DOMContentLoaded', async () => {
    startLoaderClock();
    // Карточку Апгрейда запираем/отпираем сразу по списку ADMIN_IDS, не дожидаясь
    // профиля: иначе админ видел серую карточку до ответа сервера.
    updateUpgradeAccess();
    // Покупка коинов за звёзды пока выключена — размываем карточку и кнопку «+».
    applyStarsSoonState();
    // Тап по балансу — ручная синхронизация (полезно, если Render «просыпается»).
    ['user-balance', 'profile-coins'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('click', () => refreshProfile());
    });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && !userDataLoaded) refreshProfile();
    });
    if (!tg.initData) {
        setSyncBanner('Откройте приложение через Telegram — без initData сервер не покажет баланс.');
    }
    // Задача 67: игра открыта всем, поэтому витрину тянем сразу — раньше она
    // ждала «подтверждения доступа» и при закрытом флаге не грузилась вообще.
    try { await loadUserData(); } catch(e) {}
    try { await loadShopItems(); } catch(e) {}
    hideLoader();
    // Держим сервер тёплым, пока игрок в приложении (см. API_PING_INTERVAL).
    startApiHeartbeat();
    // ---- Техработы ----
    // Решение принимаем ПОСЛЕ заставки: профиль уже пришёл, поэтому экран не мигает.
    // Задача 65: экран техработ — только если сервер прямо сказал «закрыто».
    // Если профиль не загрузился (Render спит / нет сети), показываем честное
    // «Нет связи с сервером» с кнопкой «Проверить снова» и включаем интерфейс:
    // данные всё равно придут с сервера, а игрок не упирается в тупик. Раньше
    // любая сетевая ошибка выглядела как «идёт обновление, скоро откроем».
    // Задача 67: заглушка «Технические работы» больше не показывается никогда —
    // остался только честный экран «нет связи», если сервер не ответил.
    if (!accessKnown) {
        showMaintenance({ offline: true });
        // Автоповтор: как только сервер проснётся, экран уберём сами.
        scheduleProfileRetry(4000);
    }
    // Апгрейд: синхронизация поля ввода, ползунка и быстрых кнопок.
    const upgradeAmountInput = document.getElementById('upgrade-amount');
    if (upgradeAmountInput) {
        upgradeAmountInput.addEventListener('change', syncUpgradeAmountFromInput);
        upgradeAmountInput.addEventListener('blur', syncUpgradeAmountFromInput);
    }
    const upgradeSlider = document.getElementById('upgrade-slider');
    if (upgradeSlider) {
        upgradeSlider.addEventListener('input', () => {
            if (upgradeBusy) return;
            upgradeFromAmount = Number(upgradeSlider.value) || upgradeAmountLimits().min;
            clearUpgradeResult();
            renderUpgrade();
        });
    }
    renderUpgrade();
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));

    // ---- Задача 25: новая экономика ----
    // Реферальная привязка и награда пригласившему (проверяется при входе).
    bindReferralFromStartParam().then(checkReferralStatus).catch(() => {});
    // Подписка на канал: тихая проверка. Бонус уже получен — задание скрыто;
    // иначе плашка ждёт тапа (задание придёт в личку бота).
    checkSubscription();
    // Пассивный доход НЕ забираем автоматически (задача 45): коины копятся на
    // сервере, а на баланс попадают только по кнопке «Забрать». При входе лишь
    // спрашиваем «сколько накоплено», само начисление делает тап по кнопке.
    refreshPassivePending();
    startPassiveIncomeTimer();
    // Отношения — для плашки в профиле.
    loadLoveState();
});