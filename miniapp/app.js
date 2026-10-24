const tg = window.Telegram.WebApp;
tg.expand();

let balance = 0;
let keys = 0;
let currentBet = 10;

// ВСТАВЬ СЮДА ССЫЛКУ НА ТВОЙ API НА RENDER
const API_URL = "https://dan4bot-1.onrender.com";

async function loadUserData() {
    try {
        const response = await fetch(`${API_URL}/api/user`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await response.json();
        balance = data.coins;
        updateUI();
    } catch (e) {
        console.error("Ошибка загрузки", e);
        document.getElementById('user-balance').innerText = "Error";
    }
}

async function changeBalance(amount) {
    try {
        const response = await fetch(`${API_URL}/api/update_balance`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, amount: amount })
        });
        const data = await response.json();
        balance = data.new_balance;
        updateUI();
    } catch (e) {
        console.error("Ошибка обновления", e);
    }
}

function updateUI() {
    document.getElementById('user-balance').innerText = balance;
    document.getElementById('user-keys').innerText = keys;
    document.getElementById('inventory').innerHTML = keys > 0
        ? `<div class="inv-item">🔑<br><span>Золотой ключ (${keys})</span></div>`
        : `<div class="inv-item">📦<br><span>Пусто</span></div>`;
}

loadUserData();

function switchTab(tabId, element) {
    document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
    element.classList.add('active');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
}

function openGame(game) {
    document.getElementById('main-header').style.display = 'none';
    document.getElementById('main-nav').style.display = 'none';
    document.querySelectorAll('.tab').forEach(t => t.style.display = 'none');
    document.getElementById('screen-' + game).classList.add('active');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
}

function closeGame() {
    document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active'));
    document.getElementById('main-header').style.display = 'flex';
    document.getElementById('main-nav').style.display = 'flex';
    document.getElementById('tab-play').style.display = 'block';
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
}

// ==================== ЕЖЕДНЕВНЫЙ КЕЙС ====================
let caseClaimed = false;
function claimDailyCase() {
    if (caseClaimed) return tg.showAlert("Вы уже забирали кейс сегодня!");
    const icon = document.getElementById('case-icon');
    const text = document.getElementById('case-status');

    icon.style.transform = 'scale(1.2) rotate(10deg)';
    setTimeout(() => icon.style.transform = 'scale(1.2) rotate(-10deg)', 100);
    setTimeout(() => icon.style.transform = 'scale(1.2) rotate(10deg)', 200);
    setTimeout(() => icon.style.transform = 'scale(1) rotate(0)', 300);
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');

    setTimeout(() => {
        changeBalance(50); // Начисляем 50
        icon.innerText = '💎';
        text.innerText = 'Забрано!';
        text.style.color = '#34c759';
        caseClaimed = true;
        showWin(50);
    }, 500);
}

// ==================== СУНДУКИ ====================
let normalChestAvailable = true;

function openNormalChest() {
    if (!normalChestAvailable) return tg.showAlert("Сундук перезаряжается!");
    const icon = document.getElementById('normal-chest-icon');
    const btn = document.getElementById('normal-chest-btn');

    icon.style.transform = 'scale(1.2) rotate(10deg)';
    setTimeout(() => icon.style.transform = 'scale(1) rotate(0)', 300);
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('heavy');

    setTimeout(() => {
        if (Math.random() < 0.01) {
            keys++; updateUI();
            icon.innerText = '🔑';
            showWin("ЗОЛОТОЙ КЛЮЧ!");
            if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
        } else {
            let amount = 1;
            let rand = Math.random();
            if (rand > 0.9) amount = 10;
            else if (rand > 0.7) amount = 7;
            else if (rand > 0.4) amount = 5;
            else amount = Math.floor(Math.random() * 3) + 1;

            changeBalance(amount); // Отправляем в БД
            icon.innerText = '💎';
            showWin(amount);
        }

        btn.disabled = true;
        btn.innerText = 'Перезарядка...';
        normalChestAvailable = false;

        setTimeout(() => {
            normalChestAvailable = true;
            btn.disabled = false;
            btn.innerText = 'Открыть (Бесплатно)';
            icon.innerText = '📦';
            icon.style.transform = 'scale(1)';
        }, 10000);
    }, 400);
}

function openGoldChest() {
    if (keys <= 0) return tg.showAlert("У вас нет золотых ключей!");
    const icon = document.getElementById('gold-chest-icon');
    icon.style.transform = 'scale(1.2) rotate(10deg)';
    setTimeout(() => icon.style.transform = 'scale(1) rotate(0)', 300);
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('heavy');

    setTimeout(() => {
        keys--; updateUI();
        let amount = Math.floor(Math.random() * (100 - 10 + 1)) + 10;
        changeBalance(amount); // Отправляем в БД
        icon.innerText = '💎';
        showWin(amount);
        setTimeout(() => icon.innerText = '🎁', 2000);
    }, 400);
}

function showWin(text) {
    const popup = document.getElementById('win-popup');
    popup.querySelector('.win-text').innerText = text;
    popup.classList.add('show');
    setTimeout(() => popup.classList.remove('show'), 1500);
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
}

function setBet(game, amount) {
    currentBet = amount;
    const container = document.getElementById(game + '-bet-selector');
    container.querySelectorAll('.bet-btn').forEach(btn => btn.classList.remove('active'));
    event.target.classList.add('active');
    if (game === 'mines' && !minesActive) document.getElementById('mines-btn').innerText = `Сделать ставку (${amount} 🪙)`;
    if (game === 'crash' && !crashActive) document.getElementById('crash-btn').innerText = `Сделать ставку (${amount} 🪙)`;
}

// ==================== МИНЫ ====================
let minesActive = false;
let minesCoef = 1.0;
let minesSafeOpened = 0;
const board = document.getElementById('mines-board');

for (let i = 0; i < 16; i++) {
    const cell = document.createElement('div');
    cell.className = 'cell';
    cell.dataset.index = i;
    cell.onclick = () => clickMineCell(cell);
    board.appendChild(cell);
}

document.getElementById('mines-btn').innerText = `Сделать ставку (${currentBet} 🪙)`;

function minesAction() {
    if (minesActive) { cashoutMines(); return; }
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");

    changeBalance(-currentBet); // Снимаем ставку
    minesActive = true;
    minesCoef = 1.0;
    minesSafeOpened = 0;

    document.querySelectorAll('.cell').forEach(c => {
        c.className = 'cell';
        c.innerText = '';
        c.style.pointerEvents = 'auto';
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
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');
}

function clickMineCell(cell) {
    if (!minesActive) return;
    const idx = parseInt(cell.dataset.index);
    if (cell.classList.contains('flipped')) return;
    cell.classList.add('flipped');

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
            if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
            document.getElementById('mines-btn').innerText = `Играть снова (${currentBet} 🪙)`;
            document.getElementById('mines-btn').style.background = '#007aff';
        } else {
            cell.classList.add('good'); cell.innerText = '💎';
            minesSafeOpened++;
            minesCoef = 1.0 + (minesSafeOpened * 0.15);
            const cashout = Math.floor(currentBet * minesCoef);
            document.getElementById('mines-current-coef').innerText = `x${minesCoef.toFixed(2)}`;
            document.getElementById('mines-cashout-amount').innerText = cashout;
            document.getElementById('mines-btn').innerText = `Забрать (${cashout} 🪙)`;
            if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
        }
    }, 200);
}

function cashoutMines() {
    if (!minesActive) return;
    minesActive = false;
    const cashout = Math.floor(currentBet * minesCoef);
    changeBalance(cashout); // Начисляем выигрыш
    document.getElementById('mines-btn').innerText = `Играть снова (${currentBet} 🪙)`;
    document.getElementById('mines-btn').style.background = '#007aff';
    document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
    showWin(cashout);
}

// ==================== КРАШ ====================
let crashActive = false;
let crashInterval;
let currentCoef = 1.0;

document.getElementById('crash-btn').innerText = `Сделать ставку (${currentBet} 🪙)`;

function crashAction() {
    if (crashActive) { cashedOut(); return; }
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");

    changeBalance(-currentBet); // Снимаем ставку
    crashActive = true;
    currentCoef = 1.0;

    const box = document.getElementById('rocket-box');
    const rocket = document.getElementById('crash-rocket');
    const coefText = document.getElementById('crash-coef');
    const btn = document.getElementById('crash-btn');

    box.classList.remove('crashed', 'cashed');
    rocket.className = 'rocket-emoji';
    btn.style.background = '#34c759';
    coefText.innerText = 'x1.00';

    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('medium');

    crashInterval = setInterval(() => {
        currentCoef += 0.05 + (currentCoef / 100);
        coefText.innerText = `x${currentCoef.toFixed(2)}`;
        const winAmount = Math.floor(currentBet * currentCoef);
        btn.innerText = `Забрать (${winAmount} 🪙)`;
        rocket.style.transform = `translate(${Math.random() * 20}px, -${currentCoef * 5}px)`;
        if (Math.random() < (0.01 + currentCoef / 200)) { crashed(); }
    }, 100);
}

function cashedOut() {
    if (!crashActive) return;
    clearInterval(crashInterval);
    crashActive = false;

    const winAmount = Math.floor(currentBet * currentCoef);
    changeBalance(winAmount); // Начисляем
    document.getElementById('rocket-box').classList.add('cashed');
    document.getElementById('crash-btn').innerText = `Играть снова (${currentBet} 🪙)`;
    document.getElementById('crash-btn').style.background = '#007aff';
    showWin(winAmount);
}

function crashed() {
    if (!crashActive) return;
    clearInterval(crashInterval);
    crashActive = false;
    const box = document.getElementById('rocket-box');
    box.classList.add('crashed');
    box.querySelector('.rocket-emoji').className = 'rocket-emoji crashed';
    document.getElementById('crash-btn').innerText = `Играть снова (${currentBet} 🪙)`;
    document.getElementById('crash-btn').style.background = '#007aff';
    document.body.classList.add('shake');
    setTimeout(() => document.body.classList.remove('shake'), 500);
    if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
}