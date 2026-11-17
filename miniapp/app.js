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

// Путь вида /miniapp/ или /miniapp/index.html значит «страницу отдал aura-api».
function servedFromMiniApp(loc) {
    try {
        return /^\/miniapp(\/|$)/.test(String(loc.pathname || "/"));
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
    if (fromServer && /\.png$/i.test(fromServer)) return fromServer;
    return SHOP_IMAGE_FALLBACK[itemId] || '';
}

function inventoryImageFor(itemId, item) {
    const fromServer = item && item.inventoryImage ? String(item.inventoryImage) : '';
    if (fromServer && /\.png$/i.test(fromServer)) return fromServer;
    return INV_IMAGE_FALLBACK[itemId] || '';
}
let balance = 0, keys = 0, currentBet = 10, inventory = [], lastChestTime = 0, isProcessing = false, userDataLoaded = false;
let daysInGame = 0;
// Баланс показываем только когда он реально пришёл с сервера, иначе «…» вместо ложного 0.
let balanceKnown = false;
let profileLoadPromise = null, profileRetryTimer = null, profileRetryCount = 0, profileFailure = '';
let isAdmin = false;
// Валюта в интерфейсе — картинка монеты coin.png (эмодзи больше не используем).
const COIN_IMG_SRC = 'coin.png';
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
async function apiPost(path, payload, options) {
    const res = await fetch(`${API_URL}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.assign({ initData: tg.initData }, payload || {})),
    });
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
// Сервер при этом отвечает им 403 (см. MAINTENANCE_MODE в api.py) — обойти нельзя.
let maintenanceActive = false;       // показывать ли экран
let serverMaintenance = false;       // включены ли техработы на сервере
let accessKnown = false;             // знаем ли мы, можно ли играть
let canPlay = false;                 // итоговое решение
let maintenanceMessage = '';         // текст с сервера (если он его прислал)

// ================= НОВАЯ ЭКОНОМИКА (задача 25) =================
let passiveRates = { capsule: 0.1, uniq_capsule: 0.5, ruby: 5, diamond: 20, galactic: 50 };
let passiveTimer = null;
let subscriptionChecked = false;
let loveState = null;
let referralBound = false;
const PASSIVE_INTERVAL = 15 * 60 * 1000;      // раз в 15 минут, пока аппка открыта

function showMaintenance() {
    if (maintenanceActive) return;
    maintenanceActive = true;
    canPlay = false;
    // Останавливаем всё, что могло успеть стартовать, и прячем игровой интерфейс.
    try { stopHockeyPolling(); } catch (e) {}
    try { stopCrashPolling(); } catch (e) {}
    try { document.querySelectorAll('.game-screen').forEach(screen => screen.classList.remove('active')); } catch (e) {}
    document.body.classList.add('maintenance');
    const screen = document.getElementById('maintenance-screen');
    if (screen) {
        screen.classList.add('active');
        // Текст с сервера (если пришёл) кладём в нижнюю строку, не ломая вёрстку.
        const note = screen.querySelector('.maint-note');
        if (note && maintenanceMessage) note.textContent = maintenanceMessage;
    }
    // Заставку НЕ обрываем: приветственная анимация должна доиграть (минимум 1 с),
    // а экран техработ лежит под ней и просто проявляется, когда она гаснет.
    try { hideLoader(); } catch (e) {}
}

function hideMaintenance() {
    maintenanceActive = false;
    document.body.classList.remove('maintenance');
    const screen = document.getElementById('maintenance-screen');
    if (screen) screen.classList.remove('active');
}

// Решение «пускать или нет»: админ по списку ID или по флагу сервера.
function resolveAccess(data) {
    const byId = isAdminId(user && user.id);
    const byServer = Boolean(data && data.is_admin);
    const isAdminUser = byId || byServer;
    serverMaintenance = data && data.maintenance !== undefined ? Boolean(data.maintenance) : serverMaintenance;
    if (data && typeof data.maintenance_message === 'string' && data.maintenance_message.trim()) {
        maintenanceMessage = data.maintenance_message.trim();
    }
    if (data && typeof data.can_play === 'boolean') {
        canPlay = data.can_play;
    } else {
        // Старый сервер без поля can_play: решаем по списку админов.
        canPlay = !serverMaintenance || isAdminUser;
    }
    accessKnown = true;
    return canPlay;
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
let upgradeTarget = { kind: 'mult', value: 1.35 };          // mult | chance | item
let upgradeBusy = false, upgradeStartedAt = 0;
// Белая стрелка: угол в градусах (0 = вверх), rAF-цикл и «поколение» прогона,
// чтобы запоздавшие колбэки от прошлого спина не перетирали новый результат.
let upgradeArrowAngle = 0, upgradeArrowRaf = null, upgradeSpinToken = 0;
let upgradeFetchPending = false;     // ждём ответ сервера по текущей ставке
let upgradeBusyTimer = null;         // сторож «кнопка залипла»
const UPGRADE_DURATION = 5400;        // полный раунд: быстрый спин + плавное затухание
const UPGRADE_ARROW_SPEED = 900;      // градусов в секунду в фазе ожидания ответа сервера
const UPGRADE_ARROW_MIN_LAND = 3800;  // минимум времени на «рулеточное» затухание, мс
const UPGRADE_MIN_COINS = 50, UPGRADE_MAX_COINS = 10000, UPGRADE_MAX_CAPSULES = 50;
const UPGRADE_MAX_CHANCE = 95;
// Задача 25: x4 и x8 убраны. x1.12…x1.75 — «почти монетка», x20/x50 — риск.
const UPGRADE_MULTS = [1.12, 1.35, 1.55, 1.75, 20, 50];
// Куда именно крутится стрелка: одна CSS-кривая на весь финиш, без рывков.
const UPGRADE_LAND_MS = 5000;
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
let hockeyInterval = null, hasShownWinner = false;
// Счётчик подряд идущих неудачных опросов состояния Краша (для тихого лога).
let crashPollFailures = 0;
// Круглая арена (задача 25): вместо квадратного поля и прыгающего мяча —
// пирог из секторов и указатель, который крутится и плавно останавливается.
// Стрелка командной арены: angle — текущий угол (0..360), rafId — кадр
// непрерывного вращения, landedRound — раунд, за который стрелка уже встала
// на победителя (чтобы не «доезжать» повторно на каждом опросе).
let arenaPointer = { angle: 0, roundId: null, landedRound: null, spinning: false, rafId: null, landToken: 0 };
let arenaState = { mode: 'normal', sectors: [], teams: [], myTeam: null, players: [] };
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
    const inv = document.getElementById('inventory');
    const inventoryCounts = {};
    inventory.forEach(itemId => { inventoryCounts[itemId] = (inventoryCounts[itemId] || 0) + 1; });
    const ownedItems = Object.entries(SHOP_ITEMS).filter(([itemId]) => inventoryCounts[itemId] > 0);
    if (ownedItems.length === 0) {
        inv.innerHTML = `<div class="inv-empty">📦 Инвентарь пуст</div>`;
    } else {
        inv.innerHTML = ownedItems.map(([itemId, item]) => {
            // Картинка капсулы (PNG с прозрачным фоном), эмодзи — только запасной вариант.
            const art = inventoryImageFor(itemId, item);
            const artHtml = art
                ? `<div class="inv-art"><img src="${art}" alt="${escapeHtml(item.name)}" loading="lazy"></div>`
                : `<div class="inv-emoji">${item.emoji || '💊'}</div>`;
            return `
            <div class="inv-item">
                ${artHtml}
                <span>${escapeHtml(item.shortName || item.name)} x${inventoryCounts[itemId]}</span>
            </div>
        `;
        }).join('');
    }
    renderUpgrade();
    updateUpgradeStatsUI();
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
    keys = Number(data.keys) || 0;
    inventory = Array.isArray(data.inventory) ? data.inventory : [];
    lastChestTime = Number(data.last_chest) || 0;
    daysInGame = Math.max(0, Number(data.days_in_game) || 0);
    if (data.passive_rates && typeof data.passive_rates === 'object') {
        passiveRates = Object.assign({}, passiveRates, data.passive_rates);
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
    // Техработы: как только узнали, что игрок не админ — показываем экран и
    // дальше интерфейс не трогаем (иначе он мигнёт под заглушкой).
    if (!resolveAccess(data)) {
        updateUpgradeAccess();
        showMaintenance();
        return;
    }
    hideMaintenance();
    updateUpgradeAccess();
    updateUI();
    try { checkChestCooldown(); } catch (e) { console.error(e); }
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
        await loadUserData({ attempts: 2, retryDelay: 1500 });
    }, delay);
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
        if (result === 'ok') return true;

        // Не получилось — возможно, в коде устаревший домен сервиса (Render на
        // несуществующем имени отвечает 404 «Not Found»). Ищем живой /api/health
        // и повторяем загрузку уже по правильному адресу.
        if (await recoverApiUrl()) {
            result = await tryLoadProfile();
            if (result === 'banned') return false;
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
    if (tabId === 'top') { loadGlobalTop(); }
    if (tabId === 'profile') { loadReferralStats(); loadLoveState(); }
}

function openGame(game) {
    // Техработы: игры не открываются, даже если экран кто-то «дожал» вручную.
    if (!canPlay) { showMaintenance(); return; }
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
}

function closeGame() {
    stopHockeyPolling(); stopCrashPolling();
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
    document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('main-header').style.display = 'flex';
    const subBanner = document.getElementById('sub-banner');
    if (subBanner) subBanner.style.display = '';
    document.getElementById('main-nav').style.display = 'flex';
    switchTab('play', document.querySelectorAll('.nav-item')[0]);
}

function addBet(game, amount) { const input = document.getElementById(game + '-bet'); if (!input) return; let current = parseInt(input.value) || 10; current += amount; if (current > 100000) current = 100000; input.value = current; }
function getBet(game) { const input = document.getElementById(game + '-bet'); if (!input) return 10; let val = parseInt(input.value) || 10; if (val < 1) val = 1; if (val > 100000) val = 100000; return val; }

// ================= ЛИДЕРБОРД =================
// Вкладка «Топ» — это только список: место | имя | баланс (💎).
// Никаких таймеров сезона, призовых мест и кнопок «?».
const CHANNEL_URL = 'https://t.me/Auragameplay_Channel';

// Кнопка подписки в шапке. Внутри Telegram — нативное открытие ссылки,
// снаружи (браузер/тест) — обычный переход по href.
function openChannel(event) {
    try {
        if (tg && typeof tg.openTelegramLink === 'function') {
            if (event) event.preventDefault();
            tg.openTelegramLink(CHANNEL_URL);
        }
    } catch (e) { console.error('openTelegramLink недоступен', e); }
    // После возвращения из канала проверяем подписку и, если она есть,
    // начисляем одноразовый бонус (сервер сам решает, положен ли он).
    // Telegram возвращает в WebView не мгновенно, поэтому проверяем лесенкой:
    // 1.2 с, 3 с и ещё раз, когда вкладка снова стала видимой.
    [1200, 3000].forEach(delay => setTimeout(() => checkSubscription({ alert: true }), delay));
    const onBack = () => {
        if (document.visibilityState !== 'visible') return;
        setTimeout(() => checkSubscription({ alert: true }), 900);
    };
    document.addEventListener('visibilitychange', onBack);
    setTimeout(() => document.removeEventListener('visibilitychange', onBack), 5 * 60 * 1000);
    return true;
}

// Проверка подписки через getChatMember (на сервере) + бонус 200 коинов.
// Бонус строго одноразовый: факт начисления хранится в subscriptions_history,
// поэтому повторная подписка после отписки денег не даёт.
async function checkSubscription(options) {
    const alert = Boolean(options && options.alert);
    if (!tg.initData) {
        // Вне Telegram проверить подписку невозможно — говорим это, а не молчим.
        if (alert && tg && tg.showAlert) {
            tg.showAlert('Откройте приложение через Telegram — без initData начислить бонус нельзя.');
        }
        return;
    }
    try {
        const data = await apiPost('/api/channel_check', {}, { fallback: 'Не удалось проверить подписку' });
        subscriptionChecked = true;
        const reward = document.getElementById('sub-banner-reward');
        const banner = document.getElementById('sub-banner');
        // Акция одноразовая: забрал бонус — предложение исчезает навсегда.
        // Никакой «награды за подписку» в шапке больше не висит.
        if (data.bonus_claimed && banner) banner.hidden = true;
        // Уже забирал — Telegram больше не дёргаем (сервер тоже не дёргает),
        // поэтому никаких «Bad Request: member list is inaccessible».
        if (data.bonus_claimed) {
            if (reward) reward.textContent = '✅ Бонус за подписку уже получен';
        } else if (data.ok === false) {
            // Проверка не удалась (бот не админ канала и т.п.) — не обещаем
            // «200 коинов» молча: пишем причину прямо в плашку.
            if (reward) reward.textContent = `⚠️ ${data.detail || 'Подписку не удалось проверить'}`;
        } else if (reward) {
            reward.textContent = `🎁 Награда за подписку: ${data.bonus || 200} коинов`;
        }
        if (data.granted) {
            playSound('win');
            if (banner) banner.hidden = true;
            tg.showAlert(`🎉 Спасибо за подписку! Начислено ${data.granted} коинов.`);
            await loadUserData().catch(() => {});
        } else if (alert) {
            if (data.bonus_claimed) {
                tg.showAlert('Бонус за подписку уже получен — второй раз он не начисляется.');
            } else {
                tg.showAlert(data.detail || 'Подпишитесь на канал, чтобы получить бонус.');
            }
        } else if (data.detail && data.ok === false) {
            // Проверка не удалась (бот не админ канала и т.п.) — пишем причину
            // в плашку, а не вываливаем сырой ответ Telegram в алерт.
            if (reward) reward.textContent = `⚠️ ${data.detail}`;
        }
    } catch (e) {
        if (alert) tg.showAlert(e.message || 'Не удалось проверить подписку');
    }
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
                    <span class="lb-balance">${formatCoins(_safeCoins(u.coins))} <span class="lb-gem">💎</span></span>
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

function renderPassiveCard() {
    const rateEl = document.getElementById('passive-rate');
    const hintEl = document.getElementById('passive-hint');
    const button = document.getElementById('passive-btn');
    if (!rateEl) return;
    const rate = passiveRateFor();
    rateEl.textContent = `${rate.toFixed(1)} 🪙 / час`;
    if (hintEl) {
        hintEl.textContent = rate > 0
            ? `Твой инвентарь приносит ${rate.toFixed(1)} коинов каждый час. Начисление идёт, пока аппка открыта.`
            : 'Купи капсулы — они приносят коины каждый час.';
    }
    if (button) button.disabled = rate <= 0;
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
            if (!silent) tg.showAlert(`💤 Пассивный доход: +${data.accrued} коинов`);
            balance = Number(data.new_balance) || balance;
            balanceKnown = true;
            updateUI();
        } else if (!silent) {
            const hint = data.detail || 'Пока нечего забирать — капсулы копят коины.';
            tg.showAlert(hint);
        }
        return data;
    } catch (e) {
        if (!silent) tg.showAlert(e.message || 'Не удалось начислить пассивный доход');
        return null;
    } finally {
        if (button) { button.textContent = 'Забрать'; renderPassiveCard(); }
    }
}

function startPassiveIncomeTimer() {
    if (passiveTimer) return;
    passiveTimer = setInterval(() => {
        if (document.visibilityState === 'visible' && canPlay) claimPassiveIncome({ silent: true });
    }, PASSIVE_INTERVAL);
}

// ================= РЕФЕРАЛЫ =================
// Код приглашения — ref_<id>; ссылка вида
// https://t.me/<бот>?startapp=ref_<id>. Награда начисляется пригласившему,
// когда приглашённый заходит в Mini App (сервер проверяет его NFT/premium).
function referralCodeFor(userId) { return `ref_${Number(userId) || 0}`; }

async function bindReferralFromStartParam() {
    if (referralBound) return;
    const params = (tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
    if (!params || !String(params).toLowerCase().includes('ref')) return;
    referralBound = true;
    try {
        await apiPost('/api/referral_bind', { ref_code: params });
    } catch (e) { console.warn('Не удалось привязать реферала', e); }
}

async function checkReferralStatus() {
    if (!tg.initData) return;
    try {
        const data = await apiPost('/api/referral_check', {});
        if (data && data.paid && data.reward) {
            console.info('Реферальная награда начислена:', data.reward);
        }
    } catch (e) { console.warn('referral_check', e); }
}

async function loadReferralStats() {
    if (!tg.initData) return;
    try {
        const data = await apiPost('/api/referrals', {});
        const countEl = document.getElementById('ref-count');
        const earnedEl = document.getElementById('ref-earned');
        const linkEl = document.getElementById('ref-link');
        const detailEl = document.getElementById('ref-detail');
        if (countEl) countEl.textContent = String(data.count || 0);
        if (earnedEl) earnedEl.textContent = `${formatCoins(data.earned || 0)} 🪙`;
        const botName = (tg.initDataUnsafe && tg.initDataUnsafe.bot_username) || 'AuragameBot';
        const link = data.link
            ? data.link.replace('{bot}', botName)
            : `https://t.me/${botName}?startapp=${referralCodeFor(user && user.id)}`;
        if (linkEl) linkEl.value = link;
        if (detailEl) detailEl.textContent = data.available === false ? (data.detail || '') : '';
    } catch (e) { console.warn('referrals', e); }
}

function copyReferralLink() {
    const linkEl = document.getElementById('ref-link');
    const link = linkEl ? linkEl.value : '';
    if (!link) return;
    const copy = (text) => {
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
        } catch (e) {}
        return Promise.reject(e);
    };
    copy(link).then(() => tg.showAlert('Ссылка скопирована — отправь её другу'))
        .catch(() => tg.showAlert(`Скопируй ссылку вручную: ${link}`));
}

// ================= ОТНОШЕНИЯ =================
async function loadLoveState() {
    if (!tg.initData) return;
    try {
        loveState = await apiPost('/api/love_state', {});
        renderLoveBadge();
        if (loveState && todayStageRewards(loveState)) {
            // Этапы и награды — секретная механика: не раскрываем ни название,
            // ни сумму, только мягко сообщаем, что стало теплее.
            tg.showAlert('💗 Ваши отношения стали ещё крепче!');
            await loadUserData().catch(() => {});
        }
    } catch (e) { console.warn('love_state', e); }
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
    const partner = document.getElementById('profile-love-partner');
    const stage = document.getElementById('profile-love-stage');
    const freeText = document.getElementById('profile-love-free-text');
    const freeStageEl = document.getElementById('profile-love-free-stage');
    if (!badge && !freeBadge) return;
    const inRelationship = Boolean(loveState && loveState.in_relationship);
    if (badge) badge.hidden = !inRelationship;
    if (freeBadge) freeBadge.hidden = inRelationship;
    if (!inRelationship) {
        if (freeText) freeText.textContent = freeLoveLabel();
        // Задача 30: у одиночки своя валюта (Unlove). Показываем её только если
        // она уже накоплена — на нуле плашка остаётся короткой, как раньше.
        if (freeStageEl) {
            const unlove = Number(loveState && loveState.unlove) || 0;
            const level = Number(loveState && loveState.solo_level) || 1;
            freeStageEl.textContent = unlove > 0
                ? ` · ур. ${level} · ${formatCoins(unlove)} Unlove`
                : '';
        }
        return;
    }
    if (partner) partner.textContent = loveState.partner_name || 'партнёр';
    if (stage) {
        const score = Number(loveState.love) || 0;
        const title = loveState.stage && loveState.stage.title ? ` · ${loveState.stage.title}` : '';
        const days = Number(loveState.days) > 0 ? ` · ${loveState.days} дн.` : '';
        stage.textContent = `${title}${days} · ${score} Love`;
    }
}

// ================= ПОПОЛНЕНИЕ / TELEGRAM STARS =================
// Кнопка «+» у баланса и карточка «Telegram Stars» открывают один и тот же
// экран с пакетами. Список цен приходит с сервера (/api/stars_packages), счёт
// создаёт /api/stars_invoice, а начисление делает бот по successful_payment —
// клиент деньги не рисует.
let topUpPackages = null;
let topUpBusy = false;

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
    list.innerHTML = packages.map(pkg => `
        <button class="topup-item" data-package="${escapeHtml(pkg.id)}">
            <span class="topup-item-coins">
                <img src="coin.png" alt="монета"> ${formatCoins(pkg.coins)}
            </span>
            <span class="topup-item-price">⭐ ${Number(pkg.stars) || 0}</span>
        </button>`).join('');
    list.querySelectorAll('.topup-item').forEach(btn => {
        btn.addEventListener('click', () => buyCoinPackage(btn.dataset.package, btn));
    });
}

async function openTopUp() {
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
function runChestRoulette({ isGold, winner }) {
    return new Promise(resolve => {
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

            if (progress < 1) { requestAnimationFrame(frame); return; }

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
// Если техработы снова включат (MAINTENANCE_MODE=1), пускаем только админов —
// обычному игроку даже запрос не отправляем: сервер всё равно ответил бы 403.
const CHEST_MAINTENANCE_ALERT = 'Сундуки на тех. работах. Скоро!';

function chestAccessGranted() {
    // canPlay приходит с сервера (см. resolveAccess): на открытой игре — true
    // для всех. Если техработы снова включат — сундуки остаются у админов.
    if (canPlay) return true;
    return isAdminId(user && user.id) || Boolean(isAdmin);
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
    if (chestRewardCredited && data.new_balance != null) {
        balance = Number(data.new_balance) || 0;
        balanceKnown = true;
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
        balance = Number(data.new_balance) || 0; balanceKnown = true;
        keys = Number(data.new_keys) || 0;
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

async function loadShopItems() {
    try {
        const response = await fetch(`${API_URL}/api/shop_items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Не удалось загрузить магазин");
        shopSoldOut = Array.isArray(data.sold_out) ? data.sold_out : [];
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
                // Лимиты магазина отключены (задача 17): капсулы бесконечные,
                // счётчика «Осталось» нет вообще — ни числа, ни подписи.
                unlimited: Boolean(item.unlimited),
                // Глобальный тираж: stock — сколько осталось, stockTotal — весь тираж.
                // stock_total приходит отдельным полем (старый сервер отдавал только
                // total_limit) — берём то, что есть, чтобы «Осталось: X из Y» не пропадало.
                stock: item.unlimited || item.stock == null ? null : Number(item.stock),
                // stock_ready=false — сервер не смог прочитать/списать тираж
                // (нет миграции или RLS). Тогда «Осталось: X из Y» не показываем:
                // врать про полный тираж хуже, чем не показывать его вовсе.
                stockReady: !item.unlimited && item.stock_ready !== false && !shopStockUnreliable,
                stockTotal: item.unlimited ? null
                    : ((item.stock_total != null ? item.stock_total : item.total_limit) == null
                        ? null
                        : Number(item.stock_total != null ? item.stock_total : item.total_limit))
            };
        });
        shopItemsLoaded = true;
    } catch (e) {
        console.error("Ошибка загрузки магазина", e);
        shopItemsLoaded = false;
    }
    renderShop();
    updateUI();
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
        balance = data.new_balance; balanceKnown = true;
        inventory = data.inventory || [];
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
    return upgradeChanceIsValid(upgradeChance());
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

function syncUpgradeCustomMult() {
    const slider = document.getElementById('up-custom-range');
    const label = document.getElementById('up-custom-value');
    const chanceEl = document.getElementById('up-custom-chance');
    const isMult = upgradeTarget.kind === 'mult';
    const mult = isMult ? Number(upgradeTarget.value) : UPGRADE_MULT_MIN;
    if (slider) slider.value = String(upgradeSliderFromMult(mult));
    if (label) label.textContent = isMult ? `x${mult.toFixed(2)}` : '—';
    if (chanceEl) chanceEl.textContent = isMult
        ? `${upgradeDisplayChance(upgradeMultChance(mult)).toFixed(2)}%`
        : 'выбери X';
}

// Ползунок двигают — обновляем цифры сразу (без полной перерисовки, чтобы
// палец не «дёргался»). Полная перерисовка — по окончании движения.
function onCustomMultInput(raw) {
    if (upgradeBusy) return;
    const mult = upgradeMultFromSlider(raw);
    upgradeToTab = 'coin';
    upgradeTarget = { kind: 'mult', value: mult };
    syncUpgradeCustomMult();
    document.querySelectorAll('.up-chip').forEach(chip => chip.classList.remove('active'));
    const chance = upgradeDisplayChance(upgradeMultChance(mult));
    setUpgradeRingPercent(chance);
    const chanceEl = document.getElementById('up-chance');
    if (chanceEl && !upgradeBusy) chanceEl.textContent = chance.toFixed(2) + '%';
    const outcome = document.getElementById('up-outcome');
    if (outcome) outcome.innerHTML = `Выигрыш: <b>${upgradeRewardText()}</b>`;
}

function onCustomMultCommit() {
    if (upgradeBusy) return;
    clearUpgradeResult();
    playSound('click');
    renderUpgrade();
}

function renderUpgradeFromTabs() {
    const box = document.getElementById('up-from-tabs');
    if (!box) return;
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
    const outcome = document.getElementById('up-outcome');
    if (outcome) {
        outcome.innerHTML = `Выигрыш: <b>${upgradeRewardText()}</b>`;
    }

    // --- правая панель ---
    syncUpgradeCustomMult();
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

    // --- быстрые кнопки ---
    document.querySelectorAll('.up-chip').forEach(chip => {
        const target = chip.dataset.target || '';
        const active = (upgradeTarget.kind === 'mult' && target === `mult:${upgradeTarget.value}`)
            || (upgradeTarget.kind === 'chance' && target === `chance:${upgradeTarget.value}`);
        chip.classList.toggle('active', active);
    });

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
    if (!grid) return;
    const fromValue = upgradeSourceValue();
    const tiles = [];

    if (upgradeToTab === 'coin') {
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
            if (kind === 'item') selectUpgradeTarget('item', value);
            else selectUpgradeTarget(kind, Number(value));
        });
    });
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
    if (upgradeToTab === 'coin' && upgradeTarget.kind === 'item') upgradeTarget = { kind: 'mult', value: 2 };
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

function selectUpgradeQuick(kind, value, element) {
    if (upgradeBusy) return;
    upgradeToTab = 'coin';
    document.querySelectorAll('.up-to-tab').forEach(btn => btn.classList.toggle('active', btn.id === 'up-to-tab-coin'));
    upgradeTarget = { kind, value: Number(value) };
    if (element) {
        document.querySelectorAll('.up-chip').forEach(chip => chip.classList.remove('active'));
        element.classList.add('active');
    }
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

// Доступ к Апгрейду решает СПИСОК ID (ADMIN_UI_IDS = ADMIN_IDS): у остальных
// карточка серая и неактивная, сервер дополнительно отвечает 403.
function upgradeAccessGranted() {
    // Пока профиль не пришёл, isAdmin ещё false — не мигаем «Скоро...» админу.
    if (!userDataLoaded && isAdminId(user && user.id)) return true;
    return isAdminId(user && user.id) || Boolean(isAdmin);
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
    if (sub) sub.innerText = allowed ? 'Улучшай капсулы' : 'Скоро...';
}

function updateUpgradeStatsUI() {
    // innerHTML: в текстах встречается картинка монеты.
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.innerHTML = value; };
    setText('upg-stat-total', upgradeStats.total);
    setText('upg-stat-wins', upgradeStats.wins);
    setText('upg-stat-best', upgradeStats.best > 0 ? formatCoins(upgradeStats.best) : '—');
}

async function openUpgrade() {
    // Проверяем ДО любых сетевых запросов: обычный игрок не должен даже на миг
    // увидеть экран Апгрейда. Список админов — ADMIN_IDS в app.js.
    if (!upgradeAccessGranted()) return tg.showAlert('Скоро...');
    const loaded = await ensureUserLoaded();
    if (!loaded) return tg.showAlert('Не удалось загрузить профиль. Проверьте соединение.');
    if (!upgradeAccessGranted()) {
        updateUpgradeAccess();
        return tg.showAlert('Скоро...');
    }
    // Если раунд «пропал» (ни анимации, ни ожидания ответа), не блокируем вход:
    // раньше кнопка навсегда оставалась в «КРУТИМ...» и перезаход не помогал.
    if (upgradeBusy && upgradeArrowRaf === null && !upgradeFetchPending) releaseUpgradeBusy();
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

function applyUpgradeStakePreview() {
    const amount = Math.max(0, Math.round(Number(upgradeFromAmount) || 0));
    if (!amount || upgradeStakePreview) return null;
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
    upgradeStakePreview = preview;
    updateUI();
    return preview;
}

function restoreUpgradeStakePreview() {
    const preview = upgradeStakePreview;
    upgradeStakePreview = null;
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
    if (!upgradeAccessGranted()) { updateUpgradeAccess(); return tg.showAlert('Скоро...'); }

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
        if (!isAdmin) {
            upgradeFetchPending = false;
            releaseUpgradeBusy();
            return tg.showAlert('Скоро...');
        }
        if (!upgradeCanStart()) {
            upgradeFetchPending = false;
            releaseUpgradeBusy();
            if (!upgradeChanceIsValid(upgradeChance())) {
                return tg.showAlert(upgradeChance() > UPGRADE_MAX_CHANCE
                    ? 'Шанс выше 95% — выбери цель подороже'
                    : 'Шанс слишком низкий — выбери цель подешевле');
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

    const payload = {
        initData: tg.initData,
        item_from: upgradeFrom,
        amount_from: Math.round(upgradeFromAmount),
        item_to: upgradeTarget.kind === 'item' ? upgradeTarget.value : 'coin',
        // Ключ идемпотентности: повторная отправка не спишет ставку второй раз.
        request_id: requestId()
    };
    if (upgradeTarget.kind === 'mult') payload.target_multiplier = Number(upgradeTarget.value);
    if (upgradeTarget.kind === 'chance') payload.target_chance = Number(upgradeTarget.value);

    // Ставка уходит с баланса сразу — до анимации и до ответа сервера.
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
    const abortTimer = controller ? setTimeout(() => { timedOut = true; controller.abort(); }, 8000) : null;
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
        // Ставка не сыграла — возвращаем показанный баланс/капсулы на место.
        restoreUpgradeStakePreview();
        if (circle) circle.classList.remove('spinning', 'win', 'lose');
        releaseUpgradeBusy();
        setUpgradeResult(errorText || 'Не удалось запустить апгрейд', 'error');
        if (btn) { btn.disabled = false; btn.innerText = 'АПГРЕЙД'; }
        if (unauthorized) { isAdmin = false; updateUpgradeAccess(); }
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

// Единая точка применения ответа сервера: баланс и инвентарь берём только отсюда,
// чтобы любая ветка (успех, отмена раунда, восстановление из лога) давала одно и то же.
function applyUpgradeServerState(data) {
    if (!data) return false;
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
    if (!isAdmin || !tg.initData) return null;
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
function showMissedUpgradeResult(row) {
    if (!row || upgradeBusy) return false;
    const rowId = Number(row.id);
    if (Number.isFinite(rowId)) {
        if (upgradeShownLogIds.has(rowId)) return false;
        upgradeShownLogIds.add(rowId);
    }
    const won = Boolean(row.won);
    const chance = Number(row.chance) || 0;
    lockUpgradeChance(chance);
    applyUpgradeServerState({ new_balance: row.coins_after });

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
    return showMissedUpgradeResult(data.last_unseen);
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
    cell.classList.add('flipped');
    playSound('click');
    try {
        const res = await fetch(`${API_URL}/api/mines_open`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, cell: index })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            cell.classList.remove('flipped');
            tg.showAlert(data.detail || "Не удалось открыть клетку");
            return;
        }
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
        cell.classList.remove('flipped');
        tg.showAlert("Ошибка соединения. Клетка не открыта.");
    } finally {
        isProcessing = false;
    }
}

// ================= АРЕНА (ХОККЕЙ) =================
async function placeHockeyBet(team) {
    if (isProcessing) return;
    const input = document.getElementById('hockey-bet');
    const bet = parseInt(input && input.value) || 10;
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");
    if (!isAdminId(user && user.id) && bet > 10000) return tg.showAlert("Максимальная ставка 10 000 коинов");

    isProcessing = true;
    try {
        // team передаём только в командном режиме: сервер сам проверит, что
        // игрок не ставит за обе команды в одном раунде.
        const payload = { amount: bet, request_id: requestId() };
        if (team) payload.team = team;
        const data = await apiPost('/api/place_bet', payload, { fallback: "Ставки закрыты!" });
        balance = data.new_balance; balanceKnown = true; updateUI(); playSound('click');
        if (team) { arenaState.myTeam = team; updateArenaModeUI(arenaState.mode, team); }
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
// Крутится и останавливается ровно как стрелка Апгрейда: пока идёт бросок —
// ровное вращение (requestAnimationFrame, 900°/с), на финише — несколько полных
// оборотов и плавное затухание одной CSS-кривой cubic-bezier(0.1, 0.7, 0.1, 1)
// точно на выигрышном секторе. Никаких «рывков» на стыке фаз: переход один.
const ARENA_POINTER_SPEED = 900;          // °/с — как UPGRADE_ARROW_SPEED
const ARENA_POINTER_LAND_MS = 3200;       // столько длится финишная прокрутка
const ARENA_POINTER_LAND_EASING = 'cubic-bezier(0.1, 0.7, 0.1, 1)';

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

function arenaStartPointerSpin() {
    if (arenaPointer.spinning || arenaPointer.rafId) return;
    const pointer = arenaPointerNode();
    if (!pointer) return;
    pointer.style.transition = 'none';
    pointer.classList.remove('landing');
    pointer.classList.add('spinning');
    arenaPointer.spinning = true;
    const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
    let last = nowMs();
    const step = () => {
        if (!arenaPointer.spinning) { arenaPointer.rafId = null; return; }
        const now = nowMs();
        const dt = Math.max(0, Math.min(now - last, 120)) / 1000;
        last = now;
        arenaPointer.angle = (arenaPointer.angle + ARENA_POINTER_SPEED * dt) % 360;
        arenaPointerSetAngle(arenaPointer.angle);
        arenaPointer.rafId = typeof requestAnimationFrame === 'function'
            ? requestAnimationFrame(step)
            : setTimeout(step, 16);
    };
    arenaPointer.rafId = typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame(step)
        : setTimeout(step, 16);
}

// Финиш: минимум три полных оборота + доводка до сектора победителя.
// Токен защищает от гонки: новый раунд сбрасывает стрелку, а старый
// transitionend не должен ничего «дорисовать».
function arenaLandPointer(finalX, durationMs) {
    const pointer = arenaPointerNode();
    arenaPointerStopSpin();
    if (!pointer) return;
    const token = (arenaPointer.landToken || 0) + 1;
    arenaPointer.landToken = token;

    const desired = ((arenaAngleForShare(Math.max(0, Math.min(100, Number(finalX) || 0))) % 360) + 360) % 360;
    const current = ((arenaPointer.angle % 360) + 360) % 360;
    const delta = ((desired - current) % 360 + 360) % 360;
    const total = Math.max(1200, Number(durationMs) || ARENA_POINTER_LAND_MS);
    const turns = Math.max(3, Math.ceil(total / 900));
    const target = arenaPointer.angle + turns * 360 + delta;

    let finished = false;
    let fallbackTimer = null;
    const finish = () => {
        if (finished) return;
        finished = true;
        if (pointer.removeEventListener) pointer.removeEventListener('transitionend', onTransitionEnd);
        if (fallbackTimer) clearTimeout(fallbackTimer);
        if (token !== arenaPointer.landToken) return;
        arenaPointerSetAngle(target);          // фиксируем конечный угол
    };
    const onTransitionEnd = (event) => {
        if (!event || event.target === pointer) finish();
    };

    // 1) стартовый угол без анимации, 2) reflow (иначе браузер склеит
    // transform и transition не запустится), 3) один плавный переход.
    arenaPointerSetAngle(arenaPointer.angle);
    void pointer.offsetWidth;
    pointer.classList.add('landing');
    pointer.style.transition = `transform ${total}ms ${ARENA_POINTER_LAND_EASING}`;
    arenaPointer.angle = target;
    pointer.style.transform = `rotate(${target.toFixed(2)}deg)`;
    if (pointer.addEventListener) pointer.addEventListener('transitionend', onTransitionEnd);
    fallbackTimer = setTimeout(finish, total + 300);
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

async function setArenaMode(mode) {
    try {
        const data = await apiPost('/api/arena_mode', { mode }, { fallback: 'Не удалось переключить арену' });
        arenaState.mode = data.mode || mode;
        updateArenaModeUI(arenaState.mode, null);
        // Поле с мячиком / круг переключаем сразу, не дожидаясь опроса.
        updateArenaView(arenaState.mode);
        pollHockeyState();
    } catch (e) {
        tg.showAlert(e.message || 'Не удалось переключить арену');
    }
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
        stopArenaBall();
        const fieldEl = document.getElementById('hockey-field');
        if (fieldEl) fieldEl.classList.remove('rolling', 'settled');
        if (circle) circle.classList.remove('rolling', 'settled');
    } else {
        arenaResetPointer();
        if (circle) circle.classList.remove('rolling', 'settled');
    }
}

function applyHockeyCamera(x, y, elapsedSeconds) {
    const camera = document.getElementById('hockey-camera');
    if (!camera) return;
    const span = Math.max(0.001, HOCKEY_ROLL_DURATION - ARENA_CAMERA_ZOOM_START);
    const progress = Math.max(0, Math.min(1, (Number(elapsedSeconds) - ARENA_CAMERA_ZOOM_START) / span));
    const eased = progress * progress * (3 - 2 * progress);   // smoothstep
    const scale = 1 + (ARENA_CAMERA_MAX_ZOOM - 1) * eased;
    arenaBall.zoom = scale;
    camera.style.transformOrigin = `${Math.max(0, Math.min(100, Number(x) || 0))}% ${Math.max(0, Math.min(100, Number(y) || 0))}%`;
    camera.style.transform = `scale(${scale.toFixed(4)})`;
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
        const res = await fetch(`${API_URL}/api/hockey_roll/${encodeURIComponent(roundId)}`);
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
    const startedAt = Number(arenaBall.startedAt) || 0;
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
    loadArenaRollPath(roundId).then(path => {
        if (!arenaBall.active || arenaBall.roundId !== roundId) return;
        arenaBall.path = path;
        arenaBall.loading = false;
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
    try {
        const requestStartedAt = performance.now();
        const res = await fetch(`${API_URL}/api/hockey_state`);
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

        arenaState.mode = state.mode === 'team' ? 'team' : 'normal';
        arenaState.teams = Array.isArray(state.teams) ? state.teams : [];
        arenaState.sectors = Array.isArray(state.sectors) ? state.sectors : [];
        arenaState.players = Array.isArray(state.players) ? state.players : [];
        const mine = arenaState.players.find(p => String(p.id) === String(user && user.id));
        arenaState.myTeam = mine ? (mine.team || null) : null;
        updateArenaModeUI(arenaState.mode, arenaState.myTeam);
        updateArenaView(arenaState.mode);
        const isTeamMode = arenaState.mode === 'team';

        if (state.status === 'waiting') {
            statusEl.innerText = `Ставки: ${state.timer} сек`; statusEl.style.color = '#ffcc00';
            document.getElementById('hockey-controls').style.display = 'flex';
            hasShownWinner = false;
            hideArenaBall();
            // Новый раунд — стрелка в исходное положение. Проверяем по
            // round_id, чтобы не сбрасывать её на каждом опросе.
            if (arenaPointer.roundId !== state.round_id) {
                arenaPointer.roundId = state.round_id;
                arenaResetPointer();
            }
            document.getElementById('winner-modal').classList.remove('active');
            document.getElementById('hockey-btn').disabled = false;
        } else if (state.status === 'rolling') {
            statusEl.innerText = 'Идёт бросок...'; statusEl.style.color = '#007aff';
            document.getElementById('hockey-controls').style.display = 'none';
            if (isTeamMode) {
                // Командная арена: стрелка крутится весь бросок и на финише
                // сама тормозит на выигрышном секторе (см. arenaLandPointer).
                if (arenaPointer.roundId !== state.round_id) {
                    arenaPointer.roundId = state.round_id;
                    arenaResetPointer();
                }
                arenaStartPointerSpin();
            } else if (arenaBall.roundId !== String(state.round_id)) {
                // Обычная арена: один раз на раунд берём траекторию и запускаем мяч.
                startArenaBall(state);
            }
        } else if (state.status === 'finished') {
            statusEl.innerText = 'Раунд окончен!'; statusEl.style.color = '#ff3b30';
            if (isTeamMode) {
                // Победитель уже известен серверу — стрелка доезжает до него.
                if (arenaPointer.landedRound !== state.round_id) {
                    arenaPointer.landedRound = state.round_id;
                    const finalX = state.roll_data ? state.roll_data.final_x : 0;
                    arenaLandPointer(Number(finalX) || 0, ARENA_POINTER_LAND_MS);
                }
            } else {
                freezeArenaBall(state);
            }
            const modal = document.getElementById('winner-modal');
            if (state.winner && !hasShownWinner) {
                hasShownWinner = true;
                setTimeout(() => {
                    const nameEl = document.getElementById('winner-modal-name');
                    const amountEl = document.getElementById('winner-modal-amount');
                    // Ник — текстом (не HTML), сумма — innerHTML, потому что внутри картинка монеты.
                    const teamLabel = state.winner_team
                        ? (state.winner_team === 'red' ? '🔴 Красные · ' : '🔵 Синие · ')
                        : '';
                    if (nameEl) {
                        nameEl.innerText = teamLabel + shortName(state.winner.name, 18);
                        nameEl.title = String(state.winner.name || '');
                    }
                    if (amountEl) {
                        // Ботам банк не выплачивается — им показываем «Банк», а не «+выигрыш».
                        amountEl.classList.toggle('muted', Boolean(state.winner.is_bot));
                        amountEl.innerHTML = state.winner.is_bot
                            ? `Банк: ${coinAmount(state.bank)}`
                            : `+${coinAmount(state.bank)}`;
                        amountEl.style.display = '';
                    }
                    modal.classList.add('active');

                    // The database is the only balance source; never optimistically add the pot here.
                    loadUserData();
                    if (state.winner.id === String(user.id)) { playSound('win'); } else { playSound('arena_lose'); }
                }, 1000);
            }
        }
        bankEl.innerHTML = `Банк: ${coinAmount(state.bank)}`;
        // Рисуем то поле, которое выбрано переключателем: полосы с мячиком
        // (обычная) или круг с секторами и аватарками (командная).
        if (isTeamMode) {
            if (arenaShapes(state).length || state.status === 'waiting') {
                renderArenaCircle(state);
            }
        } else {
            renderNormalArena(state);
        }
        if (arena) {
            arena.classList.toggle('team-mode', isTeamMode);
            // Анимации командной арены: «бегущее» кольцо во время броска и
            // золотая пульсация круга после него.
            arena.classList.toggle('rolling', isTeamMode && state.status === 'rolling');
            arena.classList.toggle('settled', isTeamMode && state.status === 'finished');
        }
        const list = document.getElementById('hockey-participants');
        const players = arenaState.players;
        // Список участников тоже перерисовывался каждый опрос: аватарки
        // пересоздавались 5 раз в секунду и «дёргались». Обновляем только
        // при реальном изменении состава/ставок.
        const listSignature = arenaSignature(players, state.bank);
        if (list && list.dataset.signature !== listSignature) {
            list.dataset.signature = listSignature;
            list.innerHTML = '';
            players.forEach((p, i) => {
                const div = document.createElement('div'); div.className = 'part-item';
                const width = Number(p.percent) || (state.bank ? (Number(p.bet) || 0) / state.bank * 100 : 0);
                let iconHtml = '';
                if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) { iconHtml = `<img src="${p.avatar}" style="width:20px; height:20px; border-radius:50%; margin-right:5px; vertical-align:middle;">`; }
                else { const letter = p.name ? p.name[0].toUpperCase() : '?'; iconHtml = `<span style="display:inline-flex; justify-content:center; align-items:center; width:20px; height:20px; border-radius:50%; background:${p.color}; color:white; font-size:10px; font-weight:bold; margin-right:5px;">${letter}</span>`; }
                const teamMark = p.team === 'red' ? '🔴 ' : p.team === 'blue' ? '🔵 ' : '';
                div.innerHTML = `<div class="name"><span>#${i+1}</span>${iconHtml}<span title="${escapeHtml(p.name)}">${teamMark}${escapeHtml(shortName(p.name))}</span></div><div class="stats">${width.toFixed(1)}% | ${coinAmount(p.bet)}</div>`;
                list.appendChild(div);
            });
        }
    } catch (e) { console.error(e); }
}

function startHockeyPolling() {
    if (hockeyInterval) clearInterval(hockeyInterval);
    pollHockeyState();
    hockeyInterval = setInterval(pollHockeyState, 200);
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
    try { await Promise.all([loadUserData(), loadShopItems()]); } catch(e) {}
    hideLoader();
    // Держим сервер тёплым, пока игрок в приложении (см. API_PING_INTERVAL).
    startApiHeartbeat();
    // ---- Техработы ----
    // Решение принимаем ПОСЛЕ заставки: профиль уже пришёл, поэтому экран не мигает.
    // Если профиль не загрузился (нет сети / Render спит), доступа не даём —
    // безопаснее показать заглушку, чем пустить непроверенного игрока.
    if (!accessKnown || !canPlay) {
        showMaintenance();
        return;
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
    // Подписка на канал: тихая проверка, чтобы баннер сразу показал «бонус получен».
    checkSubscription({ alert: false });
    // Пассивный доход: начисляем накопленное при входе и далее раз в 15 минут.
    claimPassiveIncome({ silent: true });
    startPassiveIncomeTimer();
    // Отношения — для плашки в профиле.
    loadLoveState();
});