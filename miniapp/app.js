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

// ВАЖНО: Замени на свой URL, если бэкенд развернут отдельно.
// Если api.py раздает статику, можно оставить пустую строку "".
const API_URL = "https://aura-api-mptt.onrender.com";

let balance = 0, keys = 0, currentBet = 10, inventory = [], lastChestTime = 0, isProcessing = false;
let hockeyInterval = null, hasBetThisRound = false, hasShownWinner = false;
let ball = { x: 50, y: 50, vx: 0, vy: 0, active: false, animationId: null, hasStartedAnim: false };

let crashInterval = null;
let lastCrashStatus = null;
let currentCrashData = null;

// Таймер лидерборда
let lbTimerEnd = Date.now() + (167 * 3600 + 59 * 60 + 59) * 1000;

// ================= ЗВУКИ =================
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function playSound(type) {
    try {
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
            const bs = audioCtx.sampleRate * 0.5;
            const b = audioCtx.createBuffer(1, bs, audioCtx.sampleRate);
            const d = b.getChannelData(0);
            for (let i = 0; i < bs; i++) d[i] = Math.random() * 2 - 1;
            const n = audioCtx.createBufferSource(); n.buffer = b;
            const ng = audioCtx.createGain(); ng.gain.setValueAtTime(0.1, now); ng.gain.exponentialRampToValueAtTime(0.01, now + 0.5);
            n.connect(ng); ng.connect(audioCtx.destination); n.start(now);
        }
    } catch(e) {}
}

// ================= ОБЩИЕ ФУНКЦИИ =================
function updateUI() {
    document.getElementById('user-balance').innerText = balance;
    document.getElementById('profile-coins').innerText = balance;
    document.getElementById('profile-keys').innerText = keys;
    document.querySelectorAll('.game-balance-val').forEach(el => { el.innerText = balance; });
    const inv = document.getElementById('inventory');
    let capCount = 0, uniqCount = 0;
    inventory.forEach(item => { if (item === 'capsule') capCount++; if (item === 'uniq_capsule') uniqCount++; });
    if (capCount === 0 && uniqCount === 0) {
        inv.innerHTML = `<div class="inv-item">📦<br><span>Пусто</span></div>`;
    } else {
        let html = '';
        if (capCount > 0) html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">💊<br><span>Капсула x${capCount}</span></div>`;
        if (uniqCount > 0) html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">⚱️<br><span>Уник. Капсула x${uniqCount}</span></div>`;
        inv.innerHTML = html;
    }
}

async function loadUserData() {
    try {
        const response = await fetch(`${API_URL}/api/user`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) });
        const data = await response.json();
        balance = data.coins; keys = data.keys || 0; inventory = data.inventory || []; lastChestTime = data.last_chest || 0;
        updateUI(); checkChestCooldown();
    } catch (e) { console.error("Ошибка загрузки профиля", e); }
}

async function changeBalance(amount) {
    try {
        const response = await fetch(`${API_URL}/api/update_balance`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) });
        const data = await response.json(); balance = data.new_balance; updateUI();
    } catch(e) {}
}

async function changeKeys(amount) {
    try {
        const response = await fetch(`${API_URL}/api/update_keys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) });
        const data = await response.json(); keys = data.new_keys; updateUI();
    } catch(e) {}
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
    if (tabId === 'top') loadGlobalTop();
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
function toggleLbInfo() {
    document.getElementById('lb-info-modal').classList.toggle('active');
}

function updateLbTimer() {
    const now = Date.now();
    let diff = Math.max(0, lbTimerEnd - now);
    const h = Math.floor(diff / 3600000); diff -= h * 3600000;
    const m = Math.floor(diff / 60000); diff -= m * 60000;
    const s = Math.floor(diff / 1000);
    document.getElementById('lb-timer-val').innerText = `${h} ч ${m} м ${s} с`;
    document.getElementById('lb-big-timer').innerText = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}
setInterval(updateLbTimer, 1000);
updateLbTimer();

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
    try {
        if (pendingChestIsGold) await changeKeys(-1);
        if (pendingChestIsKey) await changeKeys(1); else if (pendingChestReward > 0) await changeBalance(pendingChestReward);
    } catch(e) {}
    if (!pendingChestIsGold) lastChestTime = Math.floor(Date.now() / 1000);
    document.getElementById('roulette-container').style.display = 'none'; document.getElementById('chest-buttons').style.display = 'flex'; checkChestCooldown();
    pendingChestReward = 0; pendingChestIsKey = false; pendingChestIsGold = false; isProcessing = false;
}

// ================= МАГАЗИН =================
async function buyItem(itemId, price) {
    if (isProcessing) return; if (balance < price) return tg.showAlert("Недостаточно коинов!");
    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/buy_item`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, item_id: itemId, price: price }) });
        if (!res.ok) { const err = await res.json(); isProcessing = false; return tg.showAlert(err.detail || "Ошибка"); }
        const data = await res.json(); balance = data.new_balance; inventory = data.inventory; updateUI(); showWin("✅ Успешно куплено!"); playSound('click');
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

// ================= ИГРА: МИНЫ =================
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
    if (minesActive) {
        isProcessing = true; minesActive = false;
        const cashout = Math.floor(currentBet * minesCoef);
        const btn = document.getElementById('mines-btn');
        btn.disabled = true; btn.innerText = 'Забрано!'; btn.style.background = '#555';
        await changeBalance(cashout); showWin(cashout); playSound('win');
        document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
        btn.disabled = false; btn.innerText = 'Сделать ставку'; btn.style.background = '#007aff';
        isProcessing = false; return;
    }
    currentBet = getBet('mines');
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");
    isProcessing = true; await changeBalance(-currentBet); isProcessing = false;
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

// ================= ИГРА: ХОККЕЙ =================
async function placeHockeyBet() {
    if (isProcessing) return;
    if (hasBetThisRound) return tg.showAlert("Вы уже сделали ставку в этом раунде!");
    const bet = parseInt(document.getElementById('hockey-bet').value) || 10;
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");
    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/place_bet`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: bet }) });
        if (!res.ok) { isProcessing = false; return tg.showAlert("Ставки закрыты!"); }
        const data = await res.json();
        balance = data.new_balance; updateUI(); hasBetThisRound = true; playSound('click');
        document.getElementById('hockey-controls').style.display = 'none';
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

function startBallPhysics() {
    if (ball.active || ball.hasStartedAnim) return;
    ball.hasStartedAnim = true; ball.x = 50; ball.y = 50;
    let angle = Math.random() * Math.PI * 2;
    let speed = 8.0; ball.vx = Math.cos(angle) * speed; ball.vy = Math.sin(angle) * speed; ball.active = true;
    const arena = document.getElementById('hockey-arena');
    const ballEl = document.getElementById('hockey-ball');
    arena.style.transform = 'scale(1)'; arena.style.transformOrigin = 'center center';
    let scale = 1; const maxScale = 2.5; const startTime = performance.now();
    const duration = 15000; const zoomStartTime = duration - 4000;
    const step = () => {
        if (!ball.active) return;
        const elapsed = performance.now() - startTime;
        ball.x += ball.vx; ball.y += ball.vy;
        if (ball.x <= 0 || ball.x >= 100) { ball.vx *= -1; ball.x = Math.max(0, Math.min(100, ball.x)); playSound('click'); }
        if (ball.y <= 0 || ball.y >= 100) { ball.vy *= -1; ball.y = Math.max(0, Math.min(100, ball.y)); playSound('click'); }
        ball.vx *= 0.985; ball.vy *= 0.985;
        if (ballEl) { ballEl.style.left = ball.x + '%'; ballEl.style.top = ball.y + '%'; }
        if (elapsed > zoomStartTime) { scale += (maxScale - scale) * 0.02; arena.style.transform = `scale(${scale})`; arena.style.transformOrigin = `${ball.x}% ${ball.y}%`; }
        if (Math.abs(ball.vx) < 0.05 && Math.abs(ball.vy) < 0.05) {
            ball.active = false;
            fetch(`${API_URL}/api/finish_hockey`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, x: ball.x, y: ball.y }) });
            return;
        }
        ball.animationId = requestAnimationFrame(step);
    };
    ball.animationId = requestAnimationFrame(step);
}

async function pollHockeyState() {
    if (!document.getElementById('screen-hockey').classList.contains('active')) return;
    try {
        const res = await fetch(`${API_URL}/api/hockey_state`);
        const state = await res.json();
        const statusEl = document.getElementById('hockey-status');
        const bankEl = document.getElementById('hockey-bank');
        const arena = document.getElementById('hockey-arena');
        if (state.status === 'waiting') {
            statusEl.innerText = `Ставки: ${state.timer} сек`; statusEl.style.color = '#ffcc00';
            document.getElementById('hockey-controls').style.display = 'block';
            if (hasBetThisRound) hasBetThisRound = false;
            hasShownWinner = false; ball.active = false; ball.hasStartedAnim = false;
            if (ball.animationId) cancelAnimationFrame(ball.animationId);
            const ballEl = document.getElementById('hockey-ball');
            if (ballEl) { ballEl.style.left = '50%'; ballEl.style.top = '50%'; }
            if (arena) { arena.style.transform = 'scale(1)'; arena.style.transformOrigin = 'center center'; arena.querySelectorAll('.hockey-sector').forEach(s => s.style.boxShadow = 'none'); }
            document.getElementById('winner-modal').classList.remove('active');
        } else if (state.status === 'rolling') {
            statusEl.innerText = 'Идёт бросок...'; statusEl.style.color = '#007aff'; document.getElementById('hockey-controls').style.display = 'none';
            if (!ball.hasStartedAnim) startBallPhysics();
        } else if (state.status === 'finished') {
            statusEl.innerText = 'Раунд окончен!'; statusEl.style.color = '#ff3b30'; ball.active = false;
            if (ball.animationId) cancelAnimationFrame(ball.animationId);
            if (state.winner) {
                const sectors = arena.querySelectorAll('.hockey-sector');
                const winnerIndex = state.players.findIndex(p => p.id === state.winner.id);
                if (winnerIndex !== -1 && sectors[winnerIndex]) {
                    sectors[winnerIndex].style.transition = 'box-shadow 0.3s';
                    sectors[winnerIndex].style.boxShadow = 'inset 0 0 50px #fff, 0 0 30px #fff';
                    setTimeout(() => { sectors[winnerIndex].style.boxShadow = 'none'; }, 1000);
                }
            }
            const modal = document.getElementById('winner-modal');
            if (state.winner && !hasShownWinner) {
                hasShownWinner = true;
                setTimeout(() => {
                    document.getElementById('winner-modal-name').innerText = state.winner.name;
                    document.getElementById('winner-modal-amount').innerText = `+${state.bank} 🪙`;
                    modal.classList.add('active');
                    setTimeout(async () => { await loadUserData(); }, 500);
                    if (state.winner.id === String(user.id)) { playSound('win'); } else { playSound('lose'); }
                }, 1000);
            }
        }
        bankEl.innerText = `Банк: ${state.bank} 🪙`;
        arena.querySelectorAll('.hockey-sector').forEach(el => el.remove());
        let totalWidth = 0;
        state.players.forEach((p, index) => {
            const sector = document.createElement('div');
            sector.className = 'hockey-sector';
            let width = p.percent;
            if (index === state.players.length - 1) { width = 100 - totalWidth; } else { width = parseFloat(p.percent.toFixed(2)); totalWidth += width; }
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
            shapeWrap.appendChild(shape); sector.appendChild(shapeWrap); arena.appendChild(sector);
        });
        if (!document.getElementById('hockey-ball')) {
            const ballEl = document.createElement('div'); ballEl.className = 'hockey-ball'; ballEl.id = 'hockey-ball'; arena.appendChild(ballEl);
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
function stopHockeyPolling() { if (hockeyInterval) clearInterval(hockeyInterval); hockeyInterval = null; if (ball.animationId) cancelAnimationFrame(ball.animationId); }
function closeWinnerModal() { document.getElementById('winner-modal').classList.remove('active'); }

// ================= ИГРА: РАКЕТА (CRASH) =================
async function startCrashPolling() {
    if (crashInterval) clearInterval(crashInterval);
    await pollCrashState();
    crashInterval = setInterval(pollCrashState, 300);
}
function stopCrashPolling() { if (crashInterval) clearInterval(crashInterval); crashInterval = null; }

async function crashAction() {
    if (isProcessing) return;
    isProcessing = true;
    try {
        const s = currentCrashData;
        if (!s) { isProcessing = false; return; }

        if (s.my_status === 'none' || (s.my_status === 'bet' && s.status === 'waiting')) {
            const bet = getBet('crash');
            if (balance < bet) { isProcessing = false; return tg.showAlert("Недостаточно коинов!"); }
            const res = await fetch(`${API_URL}/api/crash_bet`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: tg.initData, amount: bet })
            });
            if (!res.ok) { const e = await res.json().catch(()=>({})); isProcessing = false; return tg.showAlert(e.detail || "Ошибка"); }
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
            if (!res.ok) { isProcessing = false; return tg.showAlert(data.detail || "Не успели забрать 😢"); }
            balance = data.new_balance; updateUI();
            showWin(`+${data.win_amount} 🪙`);
            playSound('win');
            await pollCrashState();
        }
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

async function pollCrashState() {
    if (!document.getElementById('screen-crash').classList.contains('active')) return;
    try {
        const url = `${API_URL}/api/crash_state?initData=${encodeURIComponent(tg.initData || '')}`;
        const res = await fetch(url);
        const state = await res.json();
        currentCrashData = state;

        const statusEl = document.getElementById('crash-status');
        const multEl = document.getElementById('crash-multiplier');
        const rocketEl = document.getElementById('crash-rocket');
        const btn = document.getElementById('crash-btn');
        const betContainer = document.getElementById('crash-bet-container');
        const historyEl = document.getElementById('crash-history-bar');

        if (state.status === 'waiting') {
            statusEl.innerText = `Ставки: ${state.timer} сек`; statusEl.style.color = '#ffcc00';
            multEl.innerText = 'x1.00'; multEl.style.color = '#fff';
            betContainer.style.display = 'block';
            rocketEl.style.left = '50px'; rocketEl.style.bottom = '50px'; rocketEl.style.transform = 'rotate(0deg)'; rocketEl.style.opacity = '1';
            document.getElementById('crash-flame').style.display = 'block'; document.getElementById('crash-explosion').style.display = 'none';
        } else if (state.status === 'ready') {
            statusEl.innerText = `Готовность: ${state.timer} сек`; statusEl.style.color = '#ff9500';
            multEl.innerText = 'x1.00'; multEl.style.color = '#fff';
            betContainer.style.display = 'none';
            rocketEl.style.left = '50px'; rocketEl.style.bottom = '50px';
        } else if (state.status === 'flying') {
            statusEl.innerText = 'Идёт полет...'; statusEl.style.color = '#34c759';
            multEl.innerText = `x${state.multiplier.toFixed(2)}`; multEl.style.color = state.multiplier > 2 ? '#ffcc00' : '#fff';
            betContainer.style.display = 'none';
            const field = document.getElementById('crash-field');
            const w = field.offsetWidth, h = field.offsetHeight;
            const t = Math.min((state.multiplier - 1) / 10, 1);
            const x = 40 + t * (w - 130);
            const y = 40 + Math.pow(t, 1.3) * (h - 130);
            rocketEl.style.left = x + 'px'; rocketEl.style.bottom = y + 'px'; rocketEl.style.transform = `rotate(${-10 - t * 35}deg)`;
        } else if (state.status === 'crashed') {
            statusEl.innerText = 'КРАШ!'; statusEl.style.color = '#ff3b30';
            multEl.innerText = `x${(state.crash_point || 0).toFixed(2)}`; multEl.style.color = '#ff3b30';
            betContainer.style.display = 'none';
            rocketEl.style.opacity = '0'; document.getElementById('crash-flame').style.display = 'none';
            const exp = document.getElementById('crash-explosion');
            exp.style.display = 'block'; exp.style.left = rocketEl.style.left; exp.style.bottom = rocketEl.style.bottom;
            if (lastCrashStatus !== 'crashed') {
                playSound('boom');
                setTimeout(() => { exp.style.display = 'none'; }, 600);
                setTimeout(async () => { await loadUserData(); }, 500);
            }
        }

        // КНОПКА СТРОГО ПО СТАТУСУ СЕРВЕРА
        if (state.status === 'waiting') {
            if (state.my_status === 'none') { btn.disabled = false; btn.innerText = 'Поставить ставку'; btn.style.background = '#007aff'; }
            else if (state.my_status === 'bet') { btn.disabled = false; btn.innerText = `Добавить (+${getBet('crash')} 🪙)`; btn.style.background = '#007aff'; }
        } else if (state.status === 'ready') {
            btn.disabled = true; btn.innerText = `Готовность ${state.timer}...`; btn.style.background = '#555';
        } else if (state.status === 'flying') {
            if (state.my_status === 'bet') {
                btn.disabled = false; btn.innerText = `Забрать (x${state.multiplier.toFixed(2)})`; btn.style.background = '#34c759';
                if (state.multiplier > 2) btn.classList.add('pulse'); else btn.classList.remove('pulse');
            } else if (state.my_status === 'cashed_out') {
                btn.disabled = true; btn.innerText = `Забрано ✅ (x${state.my_cashout_mult.toFixed(2)})`; btn.style.background = '#555'; btn.classList.remove('pulse');
            } else {
                btn.disabled = true; btn.innerText = 'Ожидание раунда...'; btn.style.background = '#555';
            }
        } else if (state.status === 'crashed') {
            btn.classList.remove('pulse');
            btn.disabled = false; btn.innerText = 'Поставить ставку'; btn.style.background = '#007aff';
        }

        if (historyEl) {
            historyEl.innerHTML = '';
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
window.addEventListener('load', async () => {
    try { await loadUserData(); } catch(e) {}
    document.querySelectorAll('.winner-modal, .chest-win-modal').forEach(m => m.classList.remove('active'));
});