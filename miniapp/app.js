const tg = window.Telegram.WebApp;
tg.expand();

const user = tg.initDataUnsafe?.user;
if (user) {
    document.getElementById('user-name').innerText = user.first_name;
    document.getElementById('profile-name').innerText = user.first_name;
    if (user.photo_url) {
        document.getElementById('user-avatar').src = user.photo_url;
        document.getElementById('profile-avatar-large').src = user.photo_url;
    }
}

const API_URL = "https://aura-api-mptt.onrender.com";
// Каталог приходит с сервера и уже отфильтрован: капсулы для админов не попадают в ответ обычным игрокам.
let SHOP_ITEMS = {};
let shopItemsLoaded = false;
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
let upgradeTarget = { kind: 'mult', value: 2 };             // mult | chance | item
let upgradeBusy = false, upgradeStartedAt = 0;
// Белая стрелка: угол в градусах (0 = вверх), rAF-цикл и «поколение» прогона,
// чтобы запоздавшие колбэки от прошлого спина не перетирали новый результат.
let upgradeArrowAngle = 0, upgradeArrowRaf = null, upgradeSpinToken = 0;
const UPGRADE_DURATION = 3000;
const UPGRADE_ARROW_SPEED = 900;      // градусов в секунду в фазе ожидания ответа сервера
const UPGRADE_ARROW_MIN_LAND = 650;   // минимум времени на торможение, мс
const UPGRADE_MIN_COINS = 50, UPGRADE_MAX_COINS = 5000, UPGRADE_MAX_CAPSULES = 50;
const UPGRADE_MAX_CHANCE = 95;
const UPGRADE_MULTS = [2, 4, 5, 8, 10];
const UPGRADE_FALLBACK_PRICES = { capsule: 500, uniq_capsule: 1000 };
const upgradeStats = { total: 0, wins: 0, best: 0 };
// Шанс, с которым реально играет текущий раунд. Пока он зафиксирован, круг и
// подпись рисуют ИМЕННО его: раньше любой updateUI() пересчитывал дугу под текущую
// форму, и стрелка могла стоять «в залёте», где на самом деле был проигрыш.
let upgradePlacedChance = null;
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
// Камера арены: плавный наезд на мяч в концовке. Арена обрезает сцену (overflow: hidden),
// поэтому увеличенное поле не вылезает за экран.
const HOCKEY_ZOOM_START = 4.5;      // с этой секунды начинаем приближать
const HOCKEY_MAX_ZOOM = 1.55;       // предел наезда
let hockeyLastThudAt = 0;
let ball = { x: 50, y: 50, active: false, animationId: null, hasStartedAnim: false, roundId: null, path: null };
let hockeyServerOffsetMs = 0, hockeyClockSynced = false;

let crashInterval = null;
let lastCrashStatus = null;
let currentCrashData = null;
let crashPollPromise = null;
let crashStateUpdatedAt = 0;

let animFrameId = null;
let isAnimatingFlight = false;
let flightStartTime = 0;

let lbTimerEnd = Date.now() + (30 * 24 * 3600 * 1000);

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
        inv.innerHTML = ownedItems.map(([itemId, item]) => `
            <div class="inv-item">
                <div class="inv-emoji">${item.emoji || '💊'}</div>
                <span>${item.shortName || item.name} x${inventoryCounts[itemId]}</span>
            </div>
        `).join('');
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
    // Список ADMIN_IDS — главный источник; флаг сервера его подтверждает.
    isAdmin = isAdminId(user && user.id) || Boolean(data.is_admin);
    userDataLoaded = true;
    balanceKnown = true;
    profileFailure = '';
    profileRetryCount = 0;
    setSyncBanner('');
    updateUpgradeAccess();
    updateUI();
    try { checkChestCooldown(); } catch (e) { console.error(e); }
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
                    return false;
                }
                if (response.ok && data && data.coins !== undefined) {
                    applyProfile(data);
                    return true;
                }
                lastError = data.detail
                    ? `${data.detail} (код ${response.status})`
                    : `Сервер ответил ошибкой ${response.status}`;
            } catch (e) {
                lastError = 'Нет связи с сервером';
            }
            if (attempt < attempts) await new Promise(r => setTimeout(r, retryDelay * attempt));
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
}

function openGame(game) {
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
    document.getElementById('main-header').style.display = 'none';
    document.getElementById('main-nav').style.display = 'none';
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    // Экран может отсутствовать, если index.html и app.js разъехались по кэшу —
    // тогда не роняем всё приложение, а тихо игнорируем клик.
    const screen = document.getElementById('screen-' + game);
    if (!screen) { console.warn('Нет экрана для игры:', game); return; }
    screen.classList.add('active');
    if (game === 'hockey') { startHockeyPolling(); }
    if (game === 'crash') { lastCrashStatus = null; startCrashPolling(); }
}

function closeGame() {
    stopHockeyPolling(); stopCrashPolling();
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
    document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('main-header').style.display = 'flex';
    document.getElementById('main-nav').style.display = 'flex';
    switchTab('play', document.querySelectorAll('.nav-item')[0]);
}

function addBet(game, amount) { const input = document.getElementById(game + '-bet'); if (!input) return; let current = parseInt(input.value) || 10; current += amount; if (current > 100000) current = 100000; input.value = current; }
function getBet(game) { const input = document.getElementById(game + '-bet'); if (!input) return 10; let val = parseInt(input.value) || 10; if (val < 1) val = 1; if (val > 100000) val = 100000; return val; }

// ================= ЛИДЕРБОРД =================
function toggleLbInfo() { document.getElementById('lb-info-modal').classList.toggle('active'); }

async function initLbTimer() {
    try {
        const res = await fetch(`${API_URL}/api/season_end`);
        const data = await res.json();
        lbTimerEnd = data.end_timestamp * 1000;
    } catch(e) { console.error("Не удалось получить время окончания сезона", e); }
    updateLbTimer();
    setInterval(updateLbTimer, 1000);
}

function updateLbTimer() {
    const now = Date.now();
    let diff = Math.max(0, lbTimerEnd - now);
    const h = Math.floor(diff / 3600000); diff -= h * 3600000;
    const m = Math.floor(diff / 60000); diff -= m * 60000;
    const s = Math.floor(diff / 1000);
    const timerVal = document.getElementById('lb-timer-val');
    const bigTimer = document.getElementById('lb-big-timer');
    if(timerVal) timerVal.innerText = `${h} ч ${m} м ${s} с`;
    if(bigTimer) bigTimer.innerText = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

async function loadGlobalTop() {
    const list = document.getElementById('global-top-list');
    if(!list) return;
    list.innerHTML = '<p style="text-align: center; color: #888;">Загрузка...</p>';
    try {
        const res = await fetch(`${API_URL}/api/global_top`);
        const payload = await res.json().catch(() => null);
        if (!res.ok) throw new Error((payload && payload.detail) || 'Топ недоступен');
        // Сервер отдаёт массив; ответ-ошибка ({detail}) не должен попадать в forEach.
        const users = Array.isArray(payload) ? payload : (Array.isArray(payload && payload.top) ? payload.top : []);
        if (users.length === 0) { list.innerHTML = '<p style="text-align: center; color: #888;">Топ пока пуст</p>'; return; }
        list.innerHTML = '';
        const prizes = ["NFT", coinAmount(10000), coinAmount(5000), coinAmount(1000), coinAmount(250), coinAmount(250), coinAmount(250)];
        users.forEach((u, i) => {
            const div = document.createElement('div');
            div.className = i < 3 ? `lb-item top-${i + 1}` : 'lb-item';
            let rankHtml = `<span class="lb-rank">${i + 1}</span>`;
            if (i === 0) rankHtml = '<span class="lb-rank lb-medal">🥇</span>';
            if (i === 1) rankHtml = '<span class="lb-rank lb-medal">🥈</span>';
            if (i === 2) rankHtml = '<span class="lb-rank lb-medal">🥉</span>';
            const prizeHtml = i < 7 ? `<span class="lb-prize">${prizes[i]}</span>` : '';
            // Сетка «ранг | ник | (коины + приз)»: ник обрезается многоточием,
            // коины и приз никогда не выходят за карточку.
            div.innerHTML = `
                <div class="lb-main">
                    ${rankHtml}
                    ${nameHtml(u.username)}
                </div>
                <div class="lb-right">
                    <span class="lb-coins">${coinAmount(u.coins)}</span>
                    ${prizeHtml}
                </div>
            `;
            list.appendChild(div);
        });
    } catch (e) { list.innerHTML = '<p style="text-align: center; color: #888;">Ошибка загрузки</p>'; }
}

// ================= СУНДУКИ =================
// Награду считает сервер (обычный сундук: 10-30 коинов или 20% на ключ; золотой — ключом),
// аппка только крутит рулетку и показывает то, что реально пришло.
let pendingChestReward = 0, pendingChestIsKey = false, pendingChestIsGold = false;

function chestRouletteItemHtml(isKey, amount) {
    return isKey ? '🔑<span>Ключ</span>' : `${COIN_IMG}<span>${amount}</span>`;
}

async function openChest(type) {
    if (isProcessing) return;
    const isGold = type === 'gold';
    if (isGold && keys <= 0) return tg.showAlert("У вас нет ключей!");
    isProcessing = true;
    let data = null;
    try {
        const response = await fetch(`${API_URL}/api/open_chest`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, chest_type: isGold ? 'gold' : 'normal' })
        });
        data = await response.json().catch(() => ({}));
        if (!response.ok) { isProcessing = false; return tg.showAlert(data.detail || "Сундук перезаряжается!"); }
    } catch (e) { isProcessing = false; return tg.showAlert("Ошибка соединения"); }

    // Награда уже начислена сервером — баланс просто синхронизируем.
    balance = Number(data.new_balance) || 0;
    balanceKnown = true;
    keys = Number(data.new_keys) || 0;
    if (data.last_chest) lastChestTime = Number(data.last_chest);
    const rewardCoins = Number(data.reward_coins) || 0;
    const rewardKeys = Number(data.reward_keys) || 0;
    const isKey = rewardKeys > 0;
    pendingChestReward = rewardCoins; pendingChestIsKey = isKey; pendingChestIsGold = isGold;
    updateUI();
    isProcessing = false;

    document.getElementById('chest-buttons').style.display = 'none';
    const roulette = document.getElementById('roulette-container'); roulette.style.display = 'block';
    const track = document.getElementById('roulette-track');
    track.style.transform = 'translateX(0)'; track.innerHTML = ''; void track.offsetWidth;

    const winIndex = Math.floor(Math.random() * 60) + 20;
    const filler = () => {
        // Соседние ячейки показывают реальный диапазон сундука, чтобы рулетка не врала.
        if (!isGold && Math.random() < 0.2) return { isKey: true, amount: 0 };
        const min = isGold ? 60 : 10, max = isGold ? 180 : 30;
        return { isKey: false, amount: Math.floor(Math.random() * (max - min + 1)) + min };
    };
    for (let i = 0; i < 100; i++) {
        const item = i === winIndex ? { isKey, amount: rewardCoins } : filler();
        const div = document.createElement('div');
        div.className = 'roulette-item' + (i === winIndex ? ' win' : '');
        div.innerHTML = chestRouletteItemHtml(item.isKey, item.amount);
        track.appendChild(div);
    }
    await new Promise(r => setTimeout(r, 50));
    const firstItem = track.firstElementChild;
    const itemWidth = firstItem ? firstItem.offsetWidth : 81;
    const windowWidth = track.parentElement ? track.parentElement.offsetWidth : 350;
    const centerOffset = (windowWidth / 2) - (itemWidth / 2);
    const targetScroll = (winIndex * itemWidth) - centerOffset;
    let start = null; const duration = 6000;
    function step(timestamp) {
        if (!start) start = timestamp;
        const progress = timestamp - start; const easeOut = 1 - Math.pow(1 - Math.min(progress / duration, 1), 4); const currentScroll = targetScroll * easeOut;
        track.style.transform = `translateX(-${currentScroll}px)`;
        if (progress < duration) { requestAnimationFrame(step); }
        else {
            track.style.transform = `translateX(-${targetScroll}px)`;
            const modal = document.getElementById('chest-win-modal');
            document.getElementById('chest-win-icon').innerHTML = isKey ? '🔑' : COIN_IMG;
            document.getElementById('chest-win-text').innerHTML = isKey
                ? 'ЗОЛОТОЙ КЛЮЧ!'
                : `Вы получили ${coinAmount(rewardCoins)}`;
            modal.classList.add('active'); playSound('win');
        }
    }
    requestAnimationFrame(step);
}

async function claimChestReward() {
    // Награда уже начислена при открытии — кнопка только закрывает окно.
    if (isProcessing) return;
    isProcessing = true;
    document.getElementById('chest-win-modal').classList.remove('active');
    document.getElementById('roulette-container').style.display = 'none';
    document.getElementById('chest-buttons').style.display = 'flex';
    checkChestCooldown();
    pendingChestReward = 0; pendingChestIsKey = false; pendingChestIsGold = false;
    isProcessing = false;
}

// ================= МАГАЗИН =================
async function loadShopItems() {
    try {
        const response = await fetch(`${API_URL}/api/shop_items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Не удалось загрузить магазин");
        SHOP_ITEMS = {};
        (data.items || []).forEach(item => {
            SHOP_ITEMS[item.id] = {
                name: item.name,
                shortName: item.short_name,
                emoji: item.emoji,
                price: Number(item.price),
                shopImage: item.shop_image,
                inventoryImage: item.inventory_image,
                dailyLimit: item.daily_limit
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
        grid.innerHTML = `<div class="inv-empty">${shopItemsLoaded ? "Магазин пуст" : "Магазин недоступен"}</div>`;
        return;
    }
    grid.innerHTML = ids.map(itemId => {
        const item = SHOP_ITEMS[itemId];
        const limitText = item.dailyLimit != null ? `До ${item.dailyLimit} шт. в день` : '';
        return `
            <article class="shop-item">
                <div class="item-icon">${item.emoji || '💊'}</div>
                <h3>${item.name}</h3>
                <p>${coinAmount(item.price)}</p>
                <small class="shop-limit">${limitText}</small>
                <button class="buy-btn" onclick="buyItem('${itemId}')">Купить</button>
            </article>
        `;
    }).join('');
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
            body: JSON.stringify({ initData: tg.initData, item_id: itemId })
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

function upgradeItemName(itemId) {
    const item = SHOP_ITEMS[itemId];
    if (item && item.name) return item.name;
    if (itemId === 'uniq_capsule') return 'Уникальная капсула';
    if (itemId === 'capsule') return 'Обычная капсула';
    return 'Коины';
}

function upgradeItemEmoji(itemId) {
    const item = SHOP_ITEMS[itemId];
    if (item && item.emoji) return item.emoji;
    if (itemId === 'uniq_capsule') return '⚱️';
    if (itemId === 'capsule') return '💊';
    return '💰';
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

// Сырой шанс = стоимость слева / стоимость цели. Не обрезаем: если он больше 95%,
// цель недостижима, и это должно быть видно и в круге, и в плитке.
function upgradeRawChance(fromValue, toValue) {
    if (!(fromValue > 0) || !(toValue > 0)) return 0;
    return (fromValue / toValue) * 100;
}

function upgradeChanceFor(fromValue, toValue) {
    return Math.min(UPGRADE_MAX_CHANCE, upgradeRawChance(fromValue, toValue));
}

function upgradeChance() { return upgradeRawChance(upgradeSourceValue(), upgradeTargetValue()); }

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
}

function releaseUpgradeChance() {
    upgradePlacedChance = null;
}

function upgradeRingChance() {
    return upgradePlacedChance === null ? upgradeChance() : upgradePlacedChance;
}

// «Выпало 63.20 из 50.00 — вне сектора»: итог можно проверить глазами,
// никаких «вроде зашло, а не засчитало».
function setUpgradeRollText(roll, chance, won) {
    const el = document.getElementById('up-roll');
    if (!el) return;
    const rollNumber = Number(roll);
    const chanceNumber = Number(chance);
    if (roll === null || roll === undefined || !Number.isFinite(rollNumber)) {
        el.textContent = 'Стрелка встанет на выпавшее число';
        el.className = 'up-roll';
        return;
    }
    const chanceText = Number.isFinite(chanceNumber) ? chanceNumber.toFixed(2) : '—';
    el.innerHTML = `Выпало <b>${rollNumber.toFixed(2)}</b> из ${chanceText} — `
        + (won ? 'в секторе успеха' : 'вне сектора');
    el.className = 'up-roll' + (won ? ' win' : ' lose');
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

function clearUpgradeResult() {
    // Любое изменение формы = новый раунд: снимаем фиксацию прошлого шанса.
    releaseUpgradeChance();
    const circle = document.getElementById('up-circle');
    if (circle) circle.classList.remove('win', 'lose');
    if (!upgradeBusy) {
        stopUpgradeArrow();
        resetUpgradeArrow();
    }
    const btn = document.getElementById('upgrade-btn');
    if (btn) btn.classList.remove('win', 'lose');
    setUpgradeResult('Выбери предмет и цель', '');
    setUpgradeRollText(null, null, false);
    clearUpgradeNet();
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

    setText('up-owned-capsule', countItem('capsule'));
    setText('up-owned-uniq', countItem('uniq_capsule'));
    const capsuleTab = document.querySelector('.up-from-tab[data-from="capsule"]');
    const uniqTab = document.querySelector('.up-from-tab[data-from="uniq_capsule"]');
    if (capsuleTab) capsuleTab.classList.toggle('empty', countItem('capsule') === 0);
    if (uniqTab) uniqTab.classList.toggle('empty', countItem('uniq_capsule') === 0);

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
        circle.style.setProperty('--p', (Math.min(chance, 100) * 3.6).toFixed(2) + 'deg');
        circle.classList.toggle('invalid', upgradePlacedChance === null && !upgradeChanceIsValid(liveChance));
    }
    if (!upgradeBusy) setText('up-chance', chance.toFixed(2) + '%');
    const outcome = document.getElementById('up-outcome');
    if (outcome) {
        outcome.innerHTML = `Выигрыш: <b>${upgradeRewardText()}</b>`;
    }

    // --- правая панель ---
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
            const hasResult = upgradeResultState === 'win' || upgradeResultState === 'lose';
            btn.disabled = !upgradeCanStart();
            // Если денег/капсул на новую ставку не хватает — не обещаем «играть заново».
            btn.innerText = (hasResult && !btn.disabled) ? 'ИГРАТЬ ЗАНОВО' : 'АПГРЕЙД';
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
            const chance = upgradeRawChance(fromValue, toValue);
            const active = upgradeTarget.kind === 'mult' && Number(upgradeTarget.value) === mult;
            tiles.push({
                key: `mult:${mult}`,
                active,
                disabled: !upgradeChanceIsValid(chance),
                html: `<div class="up-to-tile-icon">${COIN_IMG}</div>
                       <div class="up-to-tile-name">x${mult}</div>
                       <div class="up-to-tile-value">${coinAmount(toValue)}</div>
                       <div class="up-to-tile-chance">${chance.toFixed(2)}%</div>`
            });
        });
        if (upgradeTarget.kind === 'chance') {
            const toValue = upgradeTargetValue(fromValue);
            const chance = upgradeRawChance(fromValue, toValue);
            tiles.push({
                key: `chance:${upgradeTarget.value}`,
                active: true,
                html: `<div class="up-to-tile-icon">🎯</div>
                       <div class="up-to-tile-name">${Number(upgradeTarget.value).toFixed(0)}%</div>
                       <div class="up-to-tile-value">${coinAmount(toValue)}</div>
                       <div class="up-to-tile-chance">${chance.toFixed(2)}%</div>`
            });
        }
    } else {
        ['capsule', 'uniq_capsule'].forEach(itemId => {
            const toValue = upgradePrice(itemId);
            const chance = upgradeRawChance(fromValue, toValue);
            const active = upgradeTarget.kind === 'item' && upgradeTarget.value === itemId;
            const disabled = !upgradeChanceIsValid(chance);
            tiles.push({
                key: `item:${itemId}`,
                active,
                disabled,
                html: `<div class="up-to-tile-icon">${upgradeItemEmoji(itemId)}</div>
                       <div class="up-to-tile-name">${itemId === 'uniq_capsule' ? 'Уник.' : 'Обычная'}</div>
                       <div class="up-to-tile-value">${coinAmount(toValue)}</div>
                       <div class="up-to-tile-chance">${disabled ? '—' : chance.toFixed(2) + '%'}</div>`
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
    if (!['coin', 'capsule', 'uniq_capsule'].includes(type)) return;
    if (type !== 'coin' && countItem(type) === 0) {
        tg.showAlert(type === 'uniq_capsule' ? 'У тебя нет уникальных капсул' : 'У тебя нет обычных капсул');
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
function setUpgradeArrowAngle(deg) {
    upgradeArrowAngle = deg;
    const arrow = document.getElementById('up-arrow');
    if (arrow) arrow.style.transform = `rotate(${deg}deg)`;
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

// Куда встанет стрелка. Если сервер прислал выпавшее число (roll), стрелка
// показывает ИМЕННО его: бросок 0..100 → угол 0..360. Раньше позиция выбиралась
// случайно «внутри/вне сектора», поэтому стрелка и реальный бросок могли
// разойтись — игрок видел «залетело», а приза не было.
function upgradeArrowTargetFor(won, chance, rollValue) {
    const arc = Math.max(0.5, Math.min(360, (Number(chance) || 0) * 3.6));
    const rollNumber = Number(rollValue);
    if (Number.isFinite(rollNumber)) {
        let desired = Math.max(0.4, Math.min(359.6, rollNumber * 3.6));
        // Страховка на границе округления: стрелка обязана совпадать с итогом.
        if (won && desired >= arc) desired = Math.max(0.4, Math.min(arc * 0.5, arc - 0.6));
        if (!won && desired < arc) desired = Math.min(359.6, arc + 0.8);
        return desired;
    }
    if (won) {
        const margin = Math.min(arc * 0.18, Math.max(arc * 0.1, 2));
        return margin + Math.random() * Math.max(arc - margin * 2, arc * 0.25);
    }
    const free = Math.max(360 - arc, 4);
    return arc + free * 0.18 + Math.random() * free * 0.64;
}

// Фаза 2: плавно тормозим и останавливаемся ровно на выпавшем числе.
function landUpgradeArrow(won, chance, duration, token, onDone, rollValue) {
    stopUpgradeArrow();
    const desired = ((upgradeArrowTargetFor(won, chance, rollValue) % 360) + 360) % 360;

    const current = ((upgradeArrowAngle % 360) + 360) % 360;
    const delta = ((desired - current) % 360 + 360) % 360;
    const turns = duration >= 2500 ? 4 : duration >= 1200 ? 3 : 1;
    const target = upgradeArrowAngle + turns * 360 + delta;

    const startAngle = upgradeArrowAngle;
    const startTime = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const total = Math.max(0, duration);
    let finished = false;

    const complete = () => {
        if (finished) return;
        finished = true;
        // Раунд отменён — не трогаем ни стрелку, ни rAF: за это время мог
        // начаться новый спин, и страховочный таймер не должен его ломать.
        if (token !== upgradeSpinToken) return;
        stopUpgradeArrow();
        setUpgradeArrowAngle(desired);   // фиксируем точное положение
        if (typeof onDone === 'function') onDone();
    };

    // easeOutQuint: быстро в начале, ощутимое торможение в конце.
    const ease = (t) => 1 - Math.pow(1 - t, 5);
    const step = () => {
        if (token !== upgradeSpinToken) { finished = true; return; }
        const now = typeof performance !== 'undefined' ? performance.now() : Date.now();
        const progress = total > 0 ? Math.min(1, (now - startTime) / total) : 1;
        setUpgradeArrowAngle(startAngle + (target - startAngle) * ease(progress));
        if (progress >= 1) { complete(); return; }
        upgradeArrowRaf = typeof requestAnimationFrame === 'function'
            ? requestAnimationFrame(step)
            : setTimeout(() => step(), 16);
    };
    upgradeArrowRaf = typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame(step)
        : setTimeout(() => step(), 16);
    // Страховка: если вкладка была свёрнута и rAF «замёрз», завершаем по таймеру.
    setTimeout(complete, total + 260);
}

async function startUpgrade() {
    if (upgradeBusy) return;
    if (!upgradeAccessGranted()) { updateUpgradeAccess(); return tg.showAlert('Скоро...'); }

    // Блокировка ставится до любых await: иначе двойной клик во время загрузки
    // профиля отправил бы две ставки.
    upgradeBusy = true;

    try {
        const loaded = await ensureUserLoaded();
        if (!loaded) {
            upgradeBusy = false;
            return tg.showAlert('Не удалось загрузить баланс. Проверьте соединение.');
        }
        if (!isAdmin) {
            upgradeBusy = false;
            return tg.showAlert('Скоро...');
        }
        if (!upgradeCanStart()) {
            upgradeBusy = false;
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
        upgradeBusy = false;
        console.error('Ошибка подготовки апгрейда', e);
        return tg.showAlert('Не удалось подготовить апгрейд');
    }

    const payload = {
        initData: tg.initData,
        item_from: upgradeFrom,
        amount_from: Math.round(upgradeFromAmount),
        item_to: upgradeTarget.kind === 'item' ? upgradeTarget.value : 'coin'
    };
    if (upgradeTarget.kind === 'mult') payload.target_multiplier = Number(upgradeTarget.value);
    if (upgradeTarget.kind === 'chance') payload.target_chance = Number(upgradeTarget.value);

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
    }

    if (token !== upgradeSpinToken) {
        // Раунд отменён (например, открыли экран заново). Результат всё равно
        // пришёл с сервера: баланс и инвентарь надо применить, иначе игрок увидит
        // «ставку списали, приза нет».
        if (data) {
            applyUpgradeServerState(data);
            try { updateUI(); } catch (e) {}
            await loadUserData().catch(() => {});
            checkPendingUpgradeResult();
        }
        return;
    }

    // Общая длительность анимации — 3 секунды: остаток времени уходит на торможение стрелки.
    const elapsed = Date.now() - upgradeStartedAt;
    const remaining = Math.max(UPGRADE_ARROW_MIN_LAND, UPGRADE_DURATION - elapsed);

    if (errorText || !data) {
        stopUpgradeArrow();
        if (circle) circle.classList.remove('spinning', 'win', 'lose');
        upgradeBusy = false;
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
    const serverChance = Number(data.chance);
    const roundChance = Number.isFinite(serverChance) && serverChance > 0 ? serverChance : chance;
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
        circle.style.setProperty('--p', (Math.min(chance, 100) * 3.6).toFixed(2) + 'deg');
    }
    setUpgradeArrowAngle(((upgradeArrowTargetFor(won, chance, row.roll) % 360) + 360) % 360);
    setUpgradeRollText(row.roll, chance, won);
    const chanceEl = document.getElementById('up-chance');
    if (chanceEl) chanceEl.innerText = chance.toFixed(2) + '%';
    const btn = document.getElementById('upgrade-btn');
    if (btn) {
        btn.disabled = false;
        btn.classList.remove('win', 'lose');
        btn.innerText = 'АПГРЕЙД';
    }
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
            ? `${prefix}❌ ставка сгорела`
            : `${prefix}❌ капсулы сгорели`, 'lose');
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
    upgradeBusy = false;
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
        circle.style.setProperty('--p', ((Number(chance) || 0) * 3.6).toFixed(2) + 'deg');
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

    setUpgradeNetFromResult(won, data);
    setUpgradeRollText(data.roll, chance, won);

    // Диагностика честности: вердикт сервера обязан совпадать с броском.
    const rollNumber = Number(data.roll);
    const chanceNumber = Number(chance);
    if (Number.isFinite(rollNumber) && Number.isFinite(chanceNumber)
        && won !== (rollNumber < chanceNumber)) {
        console.warn('Апгрейд: бросок и вердикт расходятся', { roll: rollNumber, chance: chanceNumber, won });
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
        const lostSource = data.item_from || upgradeFrom;
        setUpgradeResult(lostSource === 'coin'
            ? '❌ Не повезло. Ставка сгорела'
            : '❌ Не повезло. Капсулы сгорели', 'lose');
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
            body: JSON.stringify({ initData: tg.initData, amount: bet, mines: selectedMines })
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
        cell.innerHTML = '💎';
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
async function placeHockeyBet() {
    if (isProcessing) return;
    const bet = parseInt(document.getElementById('hockey-bet').value) || 10;
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");
    if (!isAdminId(user && user.id) && bet > 10000) return tg.showAlert("Максимальная ставка 10 000 коинов");

    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/place_bet`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: bet }) });
        if (!res.ok) { isProcessing = false; const e = await res.json().catch(()=>({})); return tg.showAlert(e.detail || "Ставки закрыты!"); }
        const data = await res.json();
        balance = data.new_balance; balanceKnown = true; updateUI(); playSound('click');
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

const HOCKEY_ROLL_DURATION = 9.0;
const HOCKEY_SIMULATION_STEP = 0.05;
let hockeyPathCache = { roundId: null, path: null, pending: null };

async function loadHockeyPath(roundId) {
    if (hockeyPathCache.roundId === roundId && hockeyPathCache.path) return hockeyPathCache.path;
    if (hockeyPathCache.roundId === roundId && hockeyPathCache.pending) return hockeyPathCache.pending;

    hockeyPathCache = { roundId, path: null, pending: null };
    const pending = (async () => {
        const response = await fetch(`${API_URL}/api/hockey_roll/${encodeURIComponent(roundId)}`);
        if (!response.ok) throw new Error('Не удалось получить траекторию мяча');
        const data = await response.json();
        if (data.round_id !== roundId || !Array.isArray(data.path)) throw new Error('Траектория другого раунда');
        const path = data.path.map(([x, y]) => ({ x: Number(x), y: Number(y) }));
        if (hockeyPathCache.roundId === roundId) hockeyPathCache.path = path;
        return path;
    })();
    hockeyPathCache.pending = pending.catch(error => {
        if (hockeyPathCache.roundId === roundId) hockeyPathCache.pending = null;
        throw error;
    });
    return hockeyPathCache.pending;
}

function renderHockeyFinal(rollData) {
    if (!rollData) return;
    ball.x = Number(rollData.final_x);
    ball.y = Number(rollData.final_y);
    const ballEl = document.getElementById('hockey-ball');
    const scene = document.getElementById('hockey-scene');
    if (ballEl) { ballEl.style.left = `${ball.x}%`; ballEl.style.top = `${ball.y}%`; }
    if (scene) applyHockeyCamera(scene, HOCKEY_ROLL_DURATION);
}

function renderHockeyBall(elapsedSeconds) {
    const points = ball.path;
    if (!points) return;
    const framePosition = Math.max(0, Math.min(points.length - 1, elapsedSeconds / HOCKEY_SIMULATION_STEP));
    const frame = Math.floor(framePosition);
    const next = Math.min(points.length - 1, frame + 1);
    const fraction = framePosition - frame;
    ball.x = points[frame].x + (points[next].x - points[frame].x) * fraction;
    ball.y = points[frame].y + (points[next].y - points[frame].y) * fraction;

    const ballEl = document.getElementById('hockey-ball');
    const scene = document.getElementById('hockey-scene');
    if (ballEl) { ballEl.style.left = `${ball.x}%`; ballEl.style.top = `${ball.y}%`; }
    if (scene) applyHockeyCamera(scene, elapsedSeconds);
    playHockeyBounceSound(points, frame);
}

// Плавный ease-in-out наезд камеры строго на мяч: точка мяча остаётся на месте,
// поэтому поле не «уезжает» и не выходит за границы арены.
function applyHockeyCamera(scene, elapsedSeconds) {
    if (!scene) return;
    const span = Math.max(0.001, HOCKEY_ROLL_DURATION - HOCKEY_ZOOM_START);
    const progress = Math.max(0, Math.min(1, (elapsedSeconds - HOCKEY_ZOOM_START) / span));
    const eased = progress * progress * (3 - 2 * progress);
    const scale = 1 + (HOCKEY_MAX_ZOOM - 1) * eased;
    scene.style.transform = `scale(${scale.toFixed(4)})`;
    scene.style.transformOrigin = `${ball.x}% ${ball.y}%`;
}

// Глухой удар, когда мяч меняет направление (отскок от борта).
function playHockeyBounceSound(points, frame) {
    if (frame < 2 || !points[frame] || !points[frame - 1] || !points[frame - 2]) return;
    const dx1 = points[frame].x - points[frame - 1].x;
    const dx2 = points[frame - 1].x - points[frame - 2].x;
    const dy1 = points[frame].y - points[frame - 1].y;
    const dy2 = points[frame - 1].y - points[frame - 2].y;
    const bounced = (Math.abs(dx1) > 0.4 && Math.abs(dx2) > 0.4 && Math.sign(dx1) !== Math.sign(dx2))
        || (Math.abs(dy1) > 0.4 && Math.abs(dy2) > 0.4 && Math.sign(dy1) !== Math.sign(dy2));
    if (!bounced) return;
    const now = Date.now();
    if (now - hockeyLastThudAt < 140) return;   // не строчим звуком на каждом кадре
    hockeyLastThudAt = now;
    playSound('thud');
}

async function startBallPhysics(state) {
    if (ball.active && ball.roundId === state.round_id) return;
    if (ball.animationId) cancelAnimationFrame(ball.animationId);
    ball.roundId = state.round_id;
    ball.path = null;
    ball.hasStartedAnim = true;
    ball.active = true;

    try {
        const path = await loadHockeyPath(state.round_id);
        if (!ball.active || ball.roundId !== state.round_id) return;
        ball.path = path;

        const step = () => {
            if (!ball.active || ball.roundId !== state.round_id) return;
            const serverNowMs = performance.now() + hockeyServerOffsetMs;
            const elapsed = Math.max(0, (serverNowMs - Number(state.rolling_started_at) * 1000) / 1000);
            renderHockeyBall(Math.min(HOCKEY_ROLL_DURATION, elapsed));
            if (elapsed >= HOCKEY_ROLL_DURATION) {
                ball.active = false;
                return;
            }
            ball.animationId = requestAnimationFrame(step);
        };
        ball.animationId = requestAnimationFrame(step);
    } catch (error) {
        ball.active = false;
        console.error(error);
    }
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
        const scene = document.getElementById('hockey-scene');

        if (state.status === 'waiting') {
            statusEl.innerText = `Ставки: ${state.timer} сек`; statusEl.style.color = '#ffcc00';
            document.getElementById('hockey-controls').style.display = 'flex';
            hasShownWinner = false; ball.active = false; ball.hasStartedAnim = false;
            ball.roundId = null; ball.path = null;
            if (ball.animationId) cancelAnimationFrame(ball.animationId);
            const ballEl = document.getElementById('hockey-ball');
            if (ballEl) { ballEl.style.left = '50%'; ballEl.style.top = '50%'; }
            if (scene) { scene.style.transform = 'scale(1)'; scene.style.transformOrigin = 'center center'; scene.querySelectorAll('.hockey-sector').forEach(s => s.style.boxShadow = 'none'); }
            document.getElementById('winner-modal').classList.remove('active');
        } else if (state.status === 'rolling') {
            statusEl.innerText = 'Идёт бросок...'; statusEl.style.color = '#007aff'; document.getElementById('hockey-controls').style.display = 'none';
            if (!ball.active || ball.roundId !== state.round_id) startBallPhysics(state);
        } else if (state.status === 'finished') {
            statusEl.innerText = 'Раунд окончен!'; statusEl.style.color = '#ff3b30'; ball.active = false;
            if (ball.animationId) cancelAnimationFrame(ball.animationId);
            ball.roundId = state.round_id;
            if (!ball.path && hockeyPathCache.roundId === state.round_id) ball.path = hockeyPathCache.path;
            if (ball.path) renderHockeyBall(HOCKEY_ROLL_DURATION);
            else renderHockeyFinal(state.roll_data);
            const modal = document.getElementById('winner-modal');
            if (state.winner && !hasShownWinner) {
                hasShownWinner = true;
                setTimeout(() => {
                    const nameEl = document.getElementById('winner-modal-name');
                    const amountEl = document.getElementById('winner-modal-amount');
                    // Ник — текстом (не HTML), сумма — innerHTML, потому что внутри картинка монеты.
                    if (nameEl) { nameEl.innerText = shortName(state.winner.name, 18); nameEl.title = String(state.winner.name || ''); }
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
        // players может не прийти (старый сервер, частичный ответ) — тогда не
        // стираем уже нарисованную арену.
        const players = Array.isArray(state.players) ? state.players : [];
        if (!players.length && scene.querySelectorAll('.hockey-sector').length) return;
        scene.querySelectorAll('.hockey-sector').forEach(el => el.remove());
        let totalWidth = 0;
        players.forEach((p, index) => {
            const sector = document.createElement('div');
            sector.className = 'hockey-sector';
            let width = Number(p.percent) || 0;
            if (index === players.length - 1) { width = 100 - totalWidth; } else { totalWidth += width; }
            sector.style.width = width + '%'; sector.style.background = p.color; sector.style.borderRight = '1px solid rgba(0,0,0,0.5)'; sector.style.overflow = 'hidden';
            const shapeWrap = document.createElement('div'); shapeWrap.className = 'player-shape-wrapper';
            const shape = document.createElement('div'); shape.className = `player-shape shape-circle`;
            const sectorPixelWidth = (width / 100) * arena.offsetWidth;
            let size = Math.min(sectorPixelWidth * 0.7, 60); if (size < 25) size = 25;
            shape.style.width = size + 'px'; shape.style.height = size + 'px';
            if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) {
                shape.innerHTML = `<img src="${p.avatar}" style="width:100%; height:100%; object-fit:cover; border-radius:50%;">`;
                shape.style.background = 'transparent'; shape.style.border = '2px solid #fff';
            } else {
                const letter = p.name ? p.name[0].toUpperCase() : '?';
                shape.innerHTML = `<span style="color:white; font-weight:900; font-size:${size*0.5}px; text-shadow: 0 2px 4px rgba(0,0,0,0.5);">${letter}</span>`;
                shape.style.backgroundColor = p.color; shape.style.border = '2px solid rgba(255,255,255,0.5)';
            }
            shapeWrap.appendChild(shape); sector.appendChild(shapeWrap); scene.appendChild(sector);
        });
        if (!document.getElementById('hockey-ball')) {
            const ballEl = document.createElement('div'); ballEl.className = 'hockey-ball'; ballEl.id = 'hockey-ball'; scene.appendChild(ballEl);
        }
        const list = document.getElementById('hockey-participants');
        if (list) list.innerHTML = '';
        players.forEach((p, i) => {
            const div = document.createElement('div'); div.className = 'part-item';
            const width = Number(p.percent) || (state.bank ? (Number(p.bet) || 0) / state.bank * 100 : 0);
            let iconHtml = '';
            if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) { iconHtml = `<img src="${p.avatar}" style="width:20px; height:20px; border-radius:50%; margin-right:5px; vertical-align:middle;">`; }
            else { const letter = p.name ? p.name[0].toUpperCase() : '?'; iconHtml = `<span style="display:inline-flex; justify-content:center; align-items:center; width:20px; height:20px; border-radius:50%; background:${p.color}; color:white; font-size:10px; font-weight:bold; margin-right:5px;">${letter}</span>`; }
            div.innerHTML = `<div class="name"><span>#${i+1}</span>${iconHtml}<span title="${escapeHtml(p.name)}">${escapeHtml(shortName(p.name))}</span></div><div class="stats">${width.toFixed(1)}% | ${coinAmount(p.bet)}</div>`;
            list.appendChild(div);
        });
    } catch (e) { console.error(e); }
}

function startHockeyPolling() { if (hockeyInterval) clearInterval(hockeyInterval); pollHockeyState(); hockeyInterval = setInterval(pollHockeyState, 200); }
function stopHockeyPolling() {
    if (hockeyInterval) clearInterval(hockeyInterval);
    hockeyInterval = null;
    ball.active = false;
    ball.hasStartedAnim = false;
    if (ball.animationId) cancelAnimationFrame(ball.animationId);
    ball.animationId = null;
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
    initLbTimer();
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
});