const tg = window.Telegram.WebApp;
tg.expand();

// --- ЗАГРУЗКА АВАТАРКИ И НИКА ИЗ TELEGRAM ---
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

let balance = 0;
let keys = 0;
let currentBet = 10;
let inventory = [];
let lastChestTime = 0;

async function loadUserData() {
    try {
        const response = await fetch(`${API_URL}/api/user`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await response.json();
        balance = data.coins;
        keys = data.keys || 0;
        inventory = data.inventory || [];
        lastChestTime = data.last_chest || 0;

        updateUI();
        checkChestCooldown();
    } catch (e) {
        document.getElementById('user-balance').innerText = "Ошибка";
    }
}

async function changeBalance(amount) {
    const response = await fetch(`${API_URL}/api/update_balance`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData, amount: amount })
    });
    const data = await response.json();
    balance = data.new_balance;
    updateUI();
}

async function changeKeys(amount) {
    const response = await fetch(`${API_URL}/api/update_keys`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData, amount: amount })
    });
    const data = await response.json();
    keys = data.new_keys;
    updateUI();
}

function updateUI() {
    document.getElementById('user-balance').innerText = balance;
    document.getElementById('profile-coins').innerText = balance;
    document.getElementById('profile-keys').innerText = keys;

    const inv = document.getElementById('inventory');
    if (inventory.length === 0 && keys === 0) {
        inv.innerHTML = `<div class="inv-item">📦<br><span>Пусто</span></div>`;
    } else {
        let html = '';
        if (keys > 0) html += `<div class="inv-item">🔑<br><span>Ключ (${keys})</span></div>`;
        inventory.forEach(item => {
            if (item === 'capsule') html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">💊<br><span>Капсула</span></div>`;
            if (item === 'uniq_capsule') html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">⚱️<br><span>Уник. Капсула</span></div>`;
        });
        inv.innerHTML = html;
    }
}

function checkChestCooldown() {
    const now = Math.floor(Date.now() / 1000);
    const diff = now - lastChestTime;
    const btn = document.getElementById('normal-chest-btn');

    if (diff < 18000) {
        btn.disabled = true;
        let hours = Math.floor((18000 - diff) / 3600);
        let mins = Math.floor(((18000 - diff) % 3600) / 60);
        btn.innerText = `КД: ${hours}ч ${mins}м`;
        setTimeout(checkChestCooldown, 60000);
    } else {
        btn.disabled = false;
        btn.innerText = 'Открыть (Бесплатно)';
    }
}

async function openChest(type) {
    const isGold = type === 'gold';
    if (isGold && keys <= 0) return tg.showAlert("У вас нет ключей!");

    const res = await fetch(`${API_URL}/api/open_chest`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData })
    });

    if (!isGold && !res.ok) return tg.showAlert("Сундук перезаряжается!");

    document.getElementById('chest-buttons').style.display = 'none';
    const roulette = document.getElementById('roulette-container');
    roulette.style.display = 'block';

    const track = document.getElementById('roulette-track');
    track.style.transform = 'translateX(0)';
    track.innerHTML = '';

    let winIndex = 30;
    let finalAmount = 0;
    let isKey = false;

    for (let i = 0; i < 40; i++) {
        let val, icon;
        if (isGold) {
            val = Math.floor(Math.random() * 90) + 10;
            icon = '💎';
        } else {
            if (Math.random() < 0.2) { val = 0; icon = '🔑'; }
            else { val = Math.floor(Math.random() * 10) + 1; icon = '💎'; }
        }

        if (i === winIndex) {
            if (isGold) {
                finalAmount = Math.floor(Math.random() * 90) + 10;
                icon = '💎'; val = finalAmount;
            } else {
                if (Math.random() < 0.01) { isKey = true; icon = '🔑'; val = 0; }
                else { finalAmount = Math.floor(Math.random() * 10) + 1; icon = '💎'; val = finalAmount; }
            }
        }

        const div = document.createElement('div');
        div.className = 'roulette-item';
        div.innerHTML = `${icon}<span>${val > 0 ? val : 'Ключ'}</span>`;
        if (i === winIndex) div.classList.add('win');
        track.appendChild(div);
    }

    setTimeout(() => {
        const itemWidth = 81;
        const windowWidth = 350;
        const centerOffset = (windowWidth / 2) - (itemWidth / 2);
        const moveX = -((winIndex * itemWidth) - centerOffset);
        track.style.transform = `translateX(${moveX}px)`;
    }, 100);

    setTimeout(async () => {
        if (isGold) {
            await changeKeys(-1);
            await changeBalance(finalAmount);
            showWin(finalAmount);
        } else {
            if (isKey) { await changeKeys(1); showWin("КЛЮЧ!"); }
            else { await changeBalance(finalAmount); showWin(finalAmount); }
            lastChestTime = Math.floor(Date.now() / 1000);
        }
        roulette.style.display = 'none';
        document.getElementById('chest-buttons').style.display = 'flex';
        checkChestCooldown();
    }, 4200);
}

async function buyItem(itemId, price) {
    if (balance < price) return tg.showAlert("Недостаточно коинов!");
    try {
        const res = await fetch(`${API_URL}/api/buy_item`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, item_id: itemId, price: price })
        });
        if (!res.ok) return tg.showAlert("Ошибка покупки");
        const data = await res.json();
        balance = data.new_balance;
        inventory = data.inventory;
        updateUI();

        const popup = document.getElementById('win-popup');
        popup.querySelector('.win-text').innerText = "✅ Успешно куплено!";
        popup.classList.add('show');
        setTimeout(() => popup.classList.remove('show'), 2000);
        if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    } catch(e) { tg.showAlert("Ошибка соединения"); }
}

function showWin(text) {
    const popup = document.getElementById('win-popup');
    popup.querySelector('.win-text').innerText = text;
    popup.classList.add('show');
    setTimeout(() => popup.classList.remove('show'), 1500);
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
}

function switchTab(tabId, element) {
    document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
    if (element) element.classList.add('active');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
}

function closeGame() {
    document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('main-header').style.display = 'flex';
    document.getElementById('main-nav').style.display = 'flex';
    switchTab('play', document.querySelectorAll('.nav-item')[0]);
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
}

function openGame(game) {
    document.getElementById('main-header').style.display = 'none';
    document.getElementById('main-nav').style.display = 'none';
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById('screen-' + game).classList.add('active');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
}

function addBet(game, amount) {
    const input = document.getElementById(game + '-bet');
    let current = parseInt(input.value) || 10;
    current += amount;
    if (current > 10000) current = 10000;
    input.value = current;
}

function getBet(game) {
    const input = document.getElementById(game + '-bet');
    let val = parseInt(input.value) || 10;
    if (val < 10) val = 10;
    if (val > 10000) val = 10000;
    return val;
}

// --- МИНЫ ---
let minesActive = false, minesCoef = 1.0, minesSafeOpened = 0;
const board = document.getElementById('mines-board');
for (let i = 0; i < 16; i++) {
    const cell = document.createElement('div');
    cell.className = 'cell'; cell.dataset.index = i;
    cell.onclick = () => clickMineCell(cell);
    board.appendChild(cell);
}

function minesAction() {
    if (minesActive) { cashoutMines(); return; }
    currentBet = getBet('mines');
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");

    changeBalance(-currentBet);
    minesActive = true; minesCoef = 1.0; minesSafeOpened = 0;

    document.querySelectorAll('.cell').forEach(c => {
        c.className = 'cell'; c.innerText = ''; c.style.pointerEvents = 'auto';
    });

    document.getElementById('mines-btn').innerText = 'Забрать (0 🪙)';
    document.getElementById('mines-btn').style.background = '#34c759';
    document.getElementById('mines-current-coef').innerText = 'x1.00';
    document.getElementById('mines-cashout-amount').innerText = '0';

    window.mineIndices = [];
    while(window.mineIndices.length < 3) {
        let r = Math.floor(Math.random() * 16);
        if(window.mineIndices.indexOf(r) === -1) window.mineIndices.push(r);
    }
}

function clickMineCell(cell) {
    if (!minesActive) return;
    const idx = parseInt(cell.dataset.index);
    if (cell.classList.contains('flipped')) return;
    cell.classList.add('flipped');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');

    setTimeout(() => {
        if (window.mineIndices.includes(idx)) {
            cell.classList.add('bad'); cell.innerText = '💥';
            minesActive = false;
            document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
            window.mineIndices.forEach(i => {
                const c = board.children[i];
                if(!c.classList.contains('flipped')) {
                    c.classList.add('flipped', 'bad'); c.innerText = '💣';
                }
            });
            document.body.classList.add('shake');
            setTimeout(() => document.body.classList.remove('shake'), 500);
            document.getElementById('mines-btn').innerText = `Играть снова`;
            document.getElementById('mines-btn').style.background = '#007aff';
        } else {
            cell.classList.add('good'); cell.innerText = '💎';
            minesSafeOpened++;
            minesCoef = 1.0 + (minesSafeOpened * 0.15);
            const cashout = Math.floor(currentBet * minesCoef);
            document.getElementById('mines-current-coef').innerText = `x${minesCoef.toFixed(2)}`;
            document.getElementById('mines-cashout-amount').innerText = cashout;
            document.getElementById('mines-btn').innerText = `Забрать (${cashout} 🪙)`;
        }
    }, 200);
}

function cashoutMines() {
    if (!minesActive) return;
    minesActive = false;
    const cashout = Math.floor(currentBet * minesCoef);
    changeBalance(cashout);
    document.getElementById('mines-btn').innerText = `Играть снова`;
    document.getElementById('mines-btn').style.background = '#007aff';
    document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
    showWin(cashout);
}

// --- КРАШ ---
let crashActive = false, crashInterval, currentCoef = 1.0;

function crashAction() {
    if (crashActive) { cashedOut(); return; }
    currentBet = getBet('crash');
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");

    changeBalance(-currentBet);
    crashActive = true; currentCoef = 1.0;

    const box = document.getElementById('rocket-box');
    const rocket = document.getElementById('crash-rocket');
    const coefText = document.getElementById('crash-coef');
    const btn = document.getElementById('crash-btn');

    box.classList.remove('crashed', 'cashed');
    rocket.className = 'rocket-emoji';
    btn.style.background = '#34c759';
    coefText.innerText = 'x1.00';

    crashInterval = setInterval(() => {
        currentCoef += 0.02 + (currentCoef / 300);
        coefText.innerText = `x${currentCoef.toFixed(2)}`;
        const winAmount = Math.floor(currentBet * currentCoef);
        btn.innerText = `Забрать (${winAmount} 🪙)`;
        rocket.style.transform = `translate(${Math.random() * 10}px, -${currentCoef * 3}px)`;
        if (Math.random() < (0.005 + currentCoef / 300)) { crashed(); }
    }, 200);
}

function cashedOut() {
    if (!crashActive) return;
    clearInterval(crashInterval); crashActive = false;
    const winAmount = Math.floor(currentBet * currentCoef);
    changeBalance(winAmount);
    document.getElementById('rocket-box').classList.add('cashed');
    document.getElementById('crash-btn').innerText = `Играть снова`;
    document.getElementById('crash-btn').style.background = '#007aff';
    showWin(winAmount);
}

function crashed() {
    if (!crashActive) return;
    clearInterval(crashInterval); crashActive = false;
    const box = document.getElementById('rocket-box');
    box.classList.add('crashed');
    box.querySelector('.rocket-emoji').className = 'rocket-emoji crashed';
    document.getElementById('crash-btn').innerText = `Играть снова`;
    document.getElementById('crash-btn').style.background = '#007aff';
    document.body.classList.add('shake');
    setTimeout(() => document.body.classList.remove('shake'), 500);
}

loadUserData();