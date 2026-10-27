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
let isAdmin = false;
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
let hockeyInterval = null, hasShownWinner = false;
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
        } else if (type === 'boom') {
            const bs = audioCtx.sampleRate * 0.8;
            const b = audioCtx.createBuffer(1, bs, audioCtx.sampleRate);
            const d = b.getChannelData(0);
            for (let i = 0; i < bs; i++) d[i] = (Math.random() * 2 - 1) * Math.exp(-i / (bs * 0.1));
            const n = audioCtx.createBufferSource(); n.buffer = b;
            const ng = audioCtx.createGain();
            ng.gain.setValueAtTime(0.03, now);
            const filter = audioCtx.createBiquadFilter();
            filter.type = 'lowpass'; filter.frequency.setValueAtTime(400, now);
            n.connect(filter); filter.connect(ng); ng.connect(audioCtx.destination); n.start(now);
        }
    } catch(e) {}
}

// ================= ОБЩИЕ =================
function updateUI() {
    document.getElementById('user-balance').innerText = balance;
    document.getElementById('profile-coins').innerText = balance;
    document.getElementById('profile-keys').innerText = keys;
    document.querySelectorAll('.game-balance-val').forEach(el => { el.innerText = balance; });
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

async function loadUserData() {
    try {
        const response = await fetch(`${API_URL}/api/user`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) });
        const data = await response.json().catch(() => ({}));
        if (response.status === 403) {
            tg.showAlert(data.detail || "Вы забанены");
            tg.close();
            return;
        }
        if (!response.ok) throw new Error(data.detail || "Не удалось загрузить профиль");
        balance = Number(data.coins) || 0; keys = data.keys || 0; inventory = data.inventory || []; lastChestTime = data.last_chest || 0;
        isAdmin = Boolean(data.is_admin);
        userDataLoaded = true;
        updateUpgradeAccess();
        updateUI(); checkChestCooldown();
    } catch (e) { console.error("Ошибка загрузки профиля", e); }
}

async function changeBalance(amount) {
    try {
        const response = await fetch(`${API_URL}/api/update_balance`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            tg.showAlert(data.detail || "Не удалось обновить баланс. Попробуйте ещё раз.");
            return false;
        }
        const newBalance = Number(data.new_balance);
        if (!Number.isFinite(newBalance)) throw new Error("Некорректный ответ сервера");
        balance = newBalance; updateUI();
        return true;
    } catch(e) {
        console.error("Ошибка обновления баланса", e);
        tg.showAlert("Ошибка соединения. Ставка не принята, попробуйте ещё раз.");
        return false;
    }
}

async function changeKeys(amount) {
    try {
        const response = await fetch(`${API_URL}/api/update_keys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !Number.isFinite(Number(data.new_keys))) {
            tg.showAlert(data.detail || "Не удалось обновить ключи. Попробуйте ещё раз.");
            return false;
        }
        keys = Number(data.new_keys); updateUI();
        return true;
    } catch(e) {
        console.error("Ошибка обновления ключей", e);
        tg.showAlert("Ошибка соединения. Попробуйте ещё раз.");
        return false;
    }
}

function showWin(text) { const popup = document.getElementById('win-popup'); popup.querySelector('.win-text').innerText = text; popup.classList.add('show'); setTimeout(() => popup.classList.remove('show'), 1500); }

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
    document.getElementById('screen-' + game).classList.add('active');
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
        const users = await res.json();
        if (!users || users.length === 0) { list.innerHTML = '<p style="text-align: center; color: #888;">Топ пока пуст</p>'; return; }
        list.innerHTML = '';
        const prizes = ["NFT", "10000 💎", "5000 💎", "1000 💎", "250 💎", "250 💎", "250 💎"];
        users.forEach((u, i) => {
            const div = document.createElement('div');
            div.className = `lb-item top-${i + 1}`;
            if (i >= 3) div.className = 'lb-item';
            let rankHtml = `<span class="lb-rank">${i + 1}</span>`;
            if (i === 0) rankHtml = `<span class="lb-rank" style="font-size: 24px;">🥇</span>`;
            if (i === 1) rankHtml = `<span class="lb-rank" style="font-size: 24px;">🥈</span>`;
            if (i === 2) rankHtml = `<span class="lb-rank" style="font-size: 24px;">🥉</span>`;
            const prizeHtml = i < 7 ? `<span class="lb-prize">${prizes[i]}</span>` : '';
            div.innerHTML = `
                <div style="display: flex; align-items: center;">
                    ${rankHtml}
                    <span class="lb-name">${u.username || 'Игрок'}</span>
                </div>
                <div style="display: flex; align-items: center;">
                    <span class="lb-coins">💎 ${u.coins}</span>
                    ${prizeHtml}
                </div>
            `;
            list.appendChild(div);
        });
    } catch (e) { list.innerHTML = '<p style="text-align: center; color: #888;">Ошибка загрузки</p>'; }
}

// ================= СУНДУКИ =================
let pendingChestReward = 0, pendingChestIsKey = false, pendingChestIsGold = false;
async function openChest(type) {
    if (isProcessing) return;
    const isGold = type === 'gold';
    if (isGold && keys <= 0) return tg.showAlert("У вас нет ключей!");
    isProcessing = true;
    let res;
    try {
        res = await fetch(`${API_URL}/api/open_chest`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) });
    } catch(e) { isProcessing = false; return tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
    if (!isGold && !res.ok) { const err = await res.json().catch(()=>({})); return tg.showAlert(err.detail || "Сундук перезаряжается!"); }
    document.getElementById('chest-buttons').style.display = 'none';
    const roulette = document.getElementById('roulette-container'); roulette.style.display = 'block';
    const track = document.getElementById('roulette-track'); track.style.transform = 'translateX(0)'; track.innerHTML = ''; void track.offsetWidth;
    let winIndex = Math.floor(Math.random() * 60) + 20; let finalAmount = 0, isKey = false;
    for (let i = 0; i < 100; i++) {
        let val, icon;
        if (isGold) { val = Math.floor(Math.random() * 90) + 10; icon = '💎'; }
        else { if (Math.random() < 0.2) { val = 0; icon = '🔑'; } else { val = Math.floor(Math.random() * 10) + 1; icon = '💎'; } }
        if (i === winIndex) {
            if (isGold) { finalAmount = Math.floor(Math.random() * 90) + 10; icon = '💎'; val = finalAmount; }
            else { if (Math.random() < 0.01) { isKey = true; icon = '🔑'; val = 0; } else { finalAmount = Math.floor(Math.random() * 10) + 1; icon = '💎'; val = finalAmount; } }
        }
        const div = document.createElement('div'); div.className = 'roulette-item'; div.innerHTML = `${icon}<span>${val > 0 ? val : 'Ключ'}</span>`; if (i === winIndex) div.classList.add('win'); track.appendChild(div);
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
            pendingChestReward = finalAmount; pendingChestIsKey = isKey; pendingChestIsGold = isGold;
            const modal = document.getElementById('chest-win-modal');
            document.getElementById('chest-win-icon').innerText = isKey ? '🔑' : '💎';
            document.getElementById('chest-win-text').innerText = isKey ? 'ЗОЛОТОЙ КЛЮЧ!' : `Вы получили ${finalAmount} 🪙`;
            modal.classList.add('active'); playSound('win');
        }
    }
    requestAnimationFrame(step);
}

async function claimChestReward() {
    if (isProcessing) return; isProcessing = true; document.getElementById('chest-win-modal').classList.remove('active');
    let rewardApplied = true;
    if (pendingChestIsGold) rewardApplied = await changeKeys(-1);
    if (rewardApplied && pendingChestIsKey) rewardApplied = await changeKeys(1);
    else if (rewardApplied && pendingChestReward > 0) rewardApplied = await changeBalance(pendingChestReward);
    if (!rewardApplied) {
        document.getElementById('chest-win-modal').classList.add('active');
        isProcessing = false;
        return;
    }
    if (!pendingChestIsGold) lastChestTime = Math.floor(Date.now() / 1000);
    document.getElementById('roulette-container').style.display = 'none'; document.getElementById('chest-buttons').style.display = 'flex'; checkChestCooldown();
    pendingChestReward = 0; pendingChestIsKey = false; pendingChestIsGold = false; isProcessing = false;
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
                <p>${item.price} 🪙</p>
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
        balance = data.new_balance;
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
    return '🪙';
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
        return `${upgradeItemEmoji(upgradeTarget.value)} ${formatCoins(upgradePrice(upgradeTarget.value))} 🪙`;
    }
    return `${formatCoins(upgradeTargetValue(base))} 🪙`;
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

function setUpgradeResult(text, state) {
    const el = document.getElementById('upgrade-result');
    if (!el) return;
    el.innerText = text;
    el.className = 'up-result' + (state ? ' ' + state : '');
}

function clearUpgradeResult() {
    const circle = document.getElementById('up-circle');
    if (circle) circle.classList.remove('win', 'lose');
    if (!upgradeBusy) {
        stopUpgradeArrow();
        resetUpgradeArrow();
    }
    const btn = document.getElementById('upgrade-btn');
    if (btn) btn.classList.remove('win', 'lose');
    setUpgradeResult('Выбери предмет и цель', '');
}

function renderUpgrade() {
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.innerText = value; };

    ensureUpgradeSourceValid();

    // --- левая панель ---
    const fromValue = upgradeSourceValue();
    if (document.getElementById('up-from-icon')) {
        const icon = document.getElementById('up-from-icon');
        icon.innerText = upgradeFrom === 'coin' ? '💰' : upgradeItemEmoji(upgradeFrom);
    }
    setText('up-from-name', upgradeFrom === 'coin' ? 'Коины' : upgradeItemName(upgradeFrom));
    setText('up-from-value', upgradeFrom === 'coin'
        ? `${formatCoins(upgradeFromAmount)} 🪙`
        : `${upgradeFromAmount} шт. · ${formatCoins(fromValue)} 🪙`);

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
    const chance = upgradeChance();
    const circle = document.getElementById('up-circle');
    if (circle) {
        circle.style.setProperty('--p', (Math.min(chance, 100) * 3.6).toFixed(2) + 'deg');
        circle.classList.toggle('invalid', !upgradeChanceIsValid(chance));
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
    const btn = document.getElementById('upgrade-btn');
    if (btn) {
        if (upgradeBusy) {
            btn.disabled = true;
        } else {
            btn.disabled = !upgradeCanStart();
            btn.innerText = 'АПГРЕЙД';
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
                html: `<div class="up-to-tile-icon">🪙</div>
                       <div class="up-to-tile-name">x${mult}</div>
                       <div class="up-to-tile-value">${formatCoins(toValue)} 🪙</div>
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
                       <div class="up-to-tile-value">${formatCoins(toValue)} 🪙</div>
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
                       <div class="up-to-tile-value">${formatCoins(toValue)} 🪙</div>
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
    let next = value === 'max' ? limits.max : Number(value);
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

function updateUpgradeAccess() {
    const card = document.getElementById('game-card-upgrade');
    if (card) card.classList.toggle('locked', !isAdmin);
    const lock = document.getElementById('upgrade-lock-icon');
    if (lock) lock.style.display = isAdmin ? 'none' : 'inline';
    const sub = document.getElementById('upgrade-card-sub');
    if (sub) sub.innerText = isAdmin ? 'Улучшай капсулы' : 'Скоро...';
}

function updateUpgradeStatsUI() {
    const setText = (id, value) => { const el = document.getElementById(id); if (el) el.innerText = value; };
    setText('upg-stat-total', upgradeStats.total);
    setText('upg-stat-wins', upgradeStats.wins);
    setText('upg-stat-best', upgradeStats.best > 0 ? formatCoins(upgradeStats.best) : '—');
}

async function openUpgrade() {
    const loaded = await ensureUserLoaded();
    if (!loaded) return tg.showAlert('Не удалось загрузить профиль. Проверьте соединение.');
    if (!isAdmin) return tg.showAlert('Скоро...');
    upgradeSpinToken++;          // отменяем возможную недоигранную анимацию
    resetUpgradeArrow();
    openGame('upgrade');
    clearUpgradeResult();
    renderUpgrade();
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

// Фаза 2: плавно тормозим и останавливаемся ровно на нужном секторе.
function landUpgradeArrow(won, chance, duration, token, onDone) {
    stopUpgradeArrow();
    const arc = Math.max(0.5, Math.min(360, (Number(chance) || 0) * 3.6));
    let desired;
    if (won) {
        // Внутри синей дуги (шанс), с отступом от краёв.
        const margin = Math.min(arc * 0.18, Math.max(arc * 0.1, 2));
        desired = margin + Math.random() * Math.max(arc - margin * 2, arc * 0.25);
    } else {
        // Снаружи дуги — там, где сектор проигрыша.
        const free = Math.max(360 - arc, 4);
        desired = arc + free * 0.18 + Math.random() * free * 0.64;
    }
    desired = ((desired % 360) + 360) % 360;

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
        stopUpgradeArrow();
        setUpgradeArrowAngle(desired);   // фиксируем точное положение
        if (token === upgradeSpinToken && typeof onDone === 'function') onDone();
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
    upgradeStartedAt = Date.now();
    const token = ++upgradeSpinToken;   // всё, что прилетит от прошлых прогонов, игнорируем

    const circle = document.getElementById('up-circle');
    if (circle) { circle.classList.remove('win', 'lose'); circle.classList.add('spinning'); }
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

    if (token !== upgradeSpinToken) return;   // за это время запустили новый спин

    // Общая длительность анимации — 3 секунды: остаток времени уходит на торможение стрелки.
    const elapsed = Date.now() - upgradeStartedAt;
    const remaining = Math.max(UPGRADE_ARROW_MIN_LAND, UPGRADE_DURATION - elapsed);

    if (errorText || !data) {
        stopUpgradeArrow();
        if (circle) circle.classList.remove('spinning');
        upgradeBusy = false;
        if (circle) circle.classList.add('lose');
        setUpgradeResult(errorText || 'Не удалось запустить апгрейд', 'lose');
        if (btn) { btn.disabled = false; btn.innerText = 'АПГРЕЙД'; }
        if (unauthorized) { isAdmin = false; updateUpgradeAccess(); }
        tg.showAlert(errorText || 'Не удалось запустить апгрейд');
        renderUpgrade();
        // Ставка могла уйти на сервер, даже если ответ потерялся — сверяем баланс.
        if (!unauthorized) { try { await loadUserData(); } catch (e) {} }
        return;
    }

    const won = Boolean(data.success || data.won);
    landUpgradeArrow(won, chance, remaining, token, () => {
        if (circle) circle.classList.remove('spinning');
        if (token !== upgradeSpinToken) return;
        finishUpgradeAnimation(data, chance);
    });
}

// Результат уже посчитан сервером: здесь только показываем его игроку.
function finishUpgradeAnimation(data, chance) {
    if (!data) return;
    upgradeBusy = false;
    const circle = document.getElementById('up-circle');
    const btn = document.getElementById('upgrade-btn');
    const won = Boolean(data.success || data.won);

    balance = Number.isFinite(Number(data.new_balance)) ? Number(data.new_balance) : balance;
    inventory = Array.isArray(data.new_inventory) ? data.new_inventory
        : (Array.isArray(data.inventory) ? data.inventory : inventory);

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
        btn.disabled = false;
        btn.classList.remove('win', 'lose');
        btn.classList.add(won ? 'win' : 'lose');
        btn.innerText = won ? 'ПОБЕДА' : 'ПРОИГРЫШ';
    }

    if (won) {
        const text = data.win_item === 'coin'
            ? `✅ Успех! +${formatCoins(data.win_amount)} 🪙`
            : `✅ Успех! Получено: ${upgradeItemEmoji(data.win_item)} ${upgradeItemName(data.win_item)} x${data.win_amount}`;
        setUpgradeResult(text, 'win');
        showWin(data.win_item === 'coin' ? `+ ${formatCoins(data.win_amount)} 🪙` : `✅ ${upgradeItemName(data.win_item)}`);
        playSound('win');
        try { tg.HapticFeedback.notificationOccurred('success'); } catch (e) {}
    } else {
        const lostSource = data.item_from || upgradeFrom;
        setUpgradeResult(lostSource === 'coin' ? '❌ Не повезло. Ставка сгорела' : '❌ Не повезло. Капсулы сгорели', 'lose');
        playSound('lose');
        try { tg.HapticFeedback.notificationOccurred('error'); } catch (e) {}
    }

    updateUI();
}

// ================= МИНЫ =================
let minesActive = false, minesCoef = 1.0, minesSafeOpened = 0, selectedMines = 1;
const board = document.getElementById('mines-board');
if (board) {
    for (let i = 0; i < 16; i++) { const cell = document.createElement('div'); cell.className = 'cell'; cell.dataset.index = i; cell.onclick = () => clickMineCell(cell); board.appendChild(cell); }
}
const MINES_MULTIPLIERS = {
    1: [1.03, 1.10, 1.18, 1.28, 1.40, 1.55, 1.75, 2.00, 2.35, 2.80, 3.40, 4.20, 5.30, 7.00, 10.00],
    2: [1.06, 1.18, 1.35, 1.58, 1.88, 2.28, 2.82, 3.55, 4.60, 6.10, 8.30, 11.50, 16.50, 25.00, 40.00],
    3: [1.10, 1.30, 1.60, 2.00, 2.55, 3.35, 4.50, 6.20, 8.80, 13.00, 20.00, 32.00, 55.00, 100.00, 200.00],
    4: [1.15, 1.45, 1.90, 2.55, 3.50, 4.95, 7.20, 10.80, 17.00, 28.00, 48.00, 85.00, 160.00, 320.00, 700.00],
    5: [1.20, 1.60, 2.25, 3.20, 4.70, 7.20, 11.50, 19.00, 32.00, 58.00, 110.00, 220.00, 480.00, 1200.00, 3500.00]
};
function selectMines(count, element) { if (minesActive) return; selectedMines = count; document.querySelectorAll('.mine-btn').forEach(btn => btn.classList.remove('active')); element.classList.add('active'); }

async function minesAction() {
    if (isProcessing) return;
    if (!userDataLoaded) {
        await loadUserData();
        if (!userDataLoaded) return tg.showAlert("Не удалось загрузить баланс. Проверьте соединение и попробуйте снова.");
    }
    if (minesActive) {
        isProcessing = true;
        const cashout = Math.floor(currentBet * minesCoef);
        const credited = await changeBalance(cashout);
        if (!credited) { isProcessing = false; return; }
        minesActive = false;
        const btn = document.getElementById('mines-btn');
        btn.disabled = true; btn.innerText = 'Забрано!'; btn.style.background = '#555';
        showWin(cashout); playSound('win');
        document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
        btn.disabled = false; btn.innerText = 'Сделать ставку'; btn.style.background = '#007aff';
        isProcessing = false; return;
    }
    currentBet = getBet('mines');
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");
    if (user && user.id && ![6338018270, 5184813347, 8890834384].includes(Number(user.id)) && currentBet > 10000) return tg.showAlert("Максимальная ставка 10 000 🪙");
    isProcessing = true;
    const debited = await changeBalance(-currentBet);
    if (!debited) { isProcessing = false; return; }
    isProcessing = false;
    minesActive = true; minesCoef = 1.0; minesSafeOpened = 0;
    document.querySelectorAll('.cell').forEach(c => { c.className = 'cell'; c.innerText = ''; c.style.pointerEvents = 'auto'; });
    document.getElementById('mines-btn').innerText = 'Забрать (0 🪙)'; document.getElementById('mines-btn').style.background = '#34c759';
    document.getElementById('mines-current-coef').innerText = 'x1.00'; document.getElementById('mines-cashout-amount').innerText = '0';
    playSound('click');
    window.mineIndices = [];
    while(window.mineIndices.length < selectedMines) { let r = Math.floor(Math.random() * 16); if(window.mineIndices.indexOf(r) === -1) window.mineIndices.push(r); }
}

function clickMineCell(cell) {
    if (!minesActive || isProcessing) return;
    const idx = parseInt(cell.dataset.index); if (cell.classList.contains('flipped')) return;
    isProcessing = true; cell.classList.add('flipped'); playSound('click');
    setTimeout(() => {
        if (window.mineIndices.includes(idx)) {
            cell.classList.add('bad'); cell.innerText = '💥'; minesActive = false;
            document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
            window.mineIndices.forEach(i => { const c = board.children[i]; if(!c.classList.contains('flipped')) { c.classList.add('flipped', 'bad'); c.innerText = '💣'; } });
            playSound('boom');
            const btn = document.getElementById('mines-btn'); btn.innerText = `Играть снова`; btn.style.background = '#007aff';
        } else {
            cell.classList.add('good'); cell.innerText = '💎'; minesSafeOpened++;
            const multipliers = MINES_MULTIPLIERS[selectedMines];
            minesCoef = multipliers[minesSafeOpened - 1] || multipliers[multipliers.length - 1];
            const cashout = Math.floor(currentBet * minesCoef);
            document.getElementById('mines-current-coef').innerText = `x${minesCoef.toFixed(2)}`;
            document.getElementById('mines-cashout-amount').innerText = cashout;
            document.getElementById('mines-btn').innerText = `Забрать (${cashout} 🪙)`;
        }
        isProcessing = false;
    }, 200);
}

// ================= АРЕНА (ХОККЕЙ) =================
async function placeHockeyBet() {
    if (isProcessing) return;
    const bet = parseInt(document.getElementById('hockey-bet').value) || 10;
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");
    if (user && user.id && ![6338018270, 5184813347, 8890834384].includes(Number(user.id)) && bet > 10000) return tg.showAlert("Максимальная ставка 10 000 🪙");

    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/place_bet`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: bet }) });
        if (!res.ok) { isProcessing = false; const e = await res.json().catch(()=>({})); return tg.showAlert(e.detail || "Ставки закрыты!"); }
        const data = await res.json();
        balance = data.new_balance; updateUI(); playSound('click');
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
    if (scene) {
        scene.style.transform = 'scale(1.25)';
        scene.style.transformOrigin = `${ball.x}% ${ball.y}%`;
    }
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
    if (scene) {
        const zoomProgress = Math.max(0, Math.min(1, (elapsedSeconds - 6.0) / 3.0));
        const easedZoom = zoomProgress * zoomProgress * (3 - 2 * zoomProgress);
        scene.style.transform = `scale(${1 + 0.25 * easedZoom})`;
        scene.style.transformOrigin = `${ball.x}% ${ball.y}%`;
    }
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
        const state = await res.json();
        const requestFinishedAt = performance.now();
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
                    document.getElementById('winner-modal-name').innerText = state.winner.name;
                    document.getElementById('winner-modal-amount').innerText = state.winner.is_bot
                        ? ''
                        : `+${state.bank} 🪙`;
                    modal.classList.add('active');

                    // The database is the only balance source; never optimistically add the pot here.
                    loadUserData();
                    if (state.winner.id === String(user.id)) { playSound('win'); } else { playSound('lose'); }
                }, 1000);
            }
        }
        bankEl.innerText = `Банк: ${state.bank} 🪙`;
        scene.querySelectorAll('.hockey-sector').forEach(el => el.remove());
        let totalWidth = 0;
        state.players.forEach((p, index) => {
            const sector = document.createElement('div');
            sector.className = 'hockey-sector';
            let width = Number(p.percent) || 0;
            if (index === state.players.length - 1) { width = 100 - totalWidth; } else { totalWidth += width; }
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
        const list = document.getElementById('hockey-participants'); list.innerHTML = '';
        state.players.forEach((p, i) => {
            const div = document.createElement('div'); div.className = 'part-item';
            const width = p.percent || (p.bet / state.bank * 100);
            let iconHtml = '';
            if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) { iconHtml = `<img src="${p.avatar}" style="width:20px; height:20px; border-radius:50%; margin-right:5px; vertical-align:middle;">`; }
            else { const letter = p.name ? p.name[0].toUpperCase() : '?'; iconHtml = `<span style="display:inline-flex; justify-content:center; align-items:center; width:20px; height:20px; border-radius:50%; background:${p.color}; color:white; font-size:10px; font-weight:bold; margin-right:5px;">${letter}</span>`; }
            div.innerHTML = `<div class="name"><span>#${i+1}</span>${iconHtml}<span>${p.name}</span></div><div class="stats">${width.toFixed(1)}% | ${p.bet} 🪙</div>`;
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
            if (user && user.id && ![6338018270, 5184813347, 8890834384].includes(Number(user.id)) && bet > 10000) { isProcessing = false; return tg.showAlert("Максимальная ставка 10 000 🪙"); }

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
            balance = data.new_balance; updateUI();
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
            balance = data.new_balance; updateUI();
            showWin(`+${data.win_amount} 🪙`);
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
        if (!res.ok || !state.status) throw new Error(state.detail || "Не удалось получить состояние игры");
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
            else if (state.my_status === 'bet') { btn.disabled = false; btn.innerText = `Добавить (+${getBet('crash')} 🪙)`; btn.style.background = '#007aff'; }

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
                playSound('boom');
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
                if (lost) right = `<span style="color:#ff3b30;font-weight:800">💀 -${p.bet} 🪙</span>`;
                else if (cashed) { const win = Math.floor(p.bet * p.cashout_mult); right = `<span style="color:#34c759;font-weight:800">x${Number(p.cashout_mult).toFixed(2)} → +${win} 🪙</span>`; }
                else right = `<span>${p.bet} 🪙</span>`;
                d.innerHTML = `<span style="${isMe ? 'color:#007aff;font-weight:800' : ''}">${p.name}${isMe ? ' (вы)' : ''}</span>${right}`;
                if (lost) d.style.background = 'rgba(255,59,48,0.15)'; else if (cashed) d.style.background = 'rgba(52,199,89,0.10)';
                if (isMe) d.style.border = '1px solid #007aff';
                pl.appendChild(d);
            });
        }
        lastCrashStatus = state.status;
    } catch(e) { console.error(e); }
}

// ================= СТАРТ =================
document.addEventListener('DOMContentLoaded', async () => {
    try { await Promise.all([loadUserData(), loadShopItems()]); } catch(e) {}
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