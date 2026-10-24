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
    } catch (e) { document.getElementById('user-balance').innerText = "Ошибка"; }
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
    let capCount = 0;
    let uniqCount = 0;
    inventory.forEach(item => {
        if (item === 'capsule') capCount++;
        if (item === 'uniq_capsule') uniqCount++;
    });

    if (capCount === 0 && uniqCount === 0) {
        inv.innerHTML = `<div class="inv-item">📦<br><span>Пусто</span></div>`;
    } else {
        let html = '';
        if (capCount > 0) html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">💊<br><span>Капсула x${capCount}</span></div>`;
        if (uniqCount > 0) html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">⚱️<br><span>Уник. Капсула x${uniqCount}</span></div>`;
        inv.innerHTML = html;
    }
}

function checkChestCooldown() {
    const now = Math.floor(Date.now() / 1000);
    const diff = now - lastChestTime;
    const btn = document.getElementById('normal-chest-btn');
    if (diff < 18000) {
        btn.disabled = true;
        btn.innerText = `КД: ${Math.floor((18000 - diff) / 3600)}ч ${Math.floor(((18000 - diff) % 3600) / 60)}м`;
        setTimeout(checkChestCooldown, 60000);
    } else {
        btn.disabled = false; btn.innerText = 'Открыть (Бесплатно)';
    }
}

let pendingChestReward = 0, pendingChestIsKey = false, pendingChestIsGold = false;

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

    let winIndex = 50, finalAmount = 0, isKey = false;
    for (let i = 0; i < 100; i++) {
        let val, icon;
        if (isGold) { val = Math.floor(Math.random() * 90) + 10; icon = '💎'; }
        else {
            if (Math.random() < 0.2) { val = 0; icon = '🔑'; }
            else { val = Math.floor(Math.random() * 10) + 1; icon = '💎'; }
        }
        if (i === winIndex) {
            if (isGold) { finalAmount = Math.floor(Math.random() * 90) + 10; icon = '💎'; val = finalAmount; }
            else {
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
        const itemWidth = 81, windowWidth = 350;
        const centerOffset = (windowWidth / 2) - (itemWidth / 2);
        const moveX = -((winIndex * itemWidth) - centerOffset);
        track.style.transform = `translateX(${moveX}px)`;
    }, 100);

    setTimeout(() => {
        pendingChestReward = finalAmount;
        pendingChestIsKey = isKey;
        pendingChestIsGold = isGold;
        const modal = document.getElementById('chest-win-modal');
        document.getElementById('chest-win-icon').innerText = isKey ? '🔑' : '💎';
        document.getElementById('chest-win-text').innerText = isKey ? 'ЗОЛОТОЙ КЛЮЧ!' : `Вы получили ${finalAmount} 🪙`;
        modal.classList.add('active');
        if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    }, 10200);
}

async function claimChestReward() {
    document.getElementById('chest-win-modal').classList.remove('active');
    if (pendingChestIsGold) await changeKeys(-1);
    if (pendingChestIsKey) await changeKeys(1);
    else if (pendingChestReward > 0) await changeBalance(pendingChestReward);
    if (!pendingChestIsGold) lastChestTime = Math.floor(Date.now() / 1000);
    document.getElementById('roulette-container').style.display = 'none';
    document.getElementById('chest-buttons').style.display = 'flex';
    checkChestCooldown();
    pendingChestReward = 0; pendingChestIsKey = false; pendingChestIsGold = false;
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
        showWin("✅ Успешно куплено!");
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
    const popup = document.getElementById('win-popup');
    if (popup) popup.classList.remove('show');

    document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
    if (element) element.classList.add('active');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
}

function closeGame() {
    const popup = document.getElementById('win-popup');
    if (popup) popup.classList.remove('show');

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
    if (!input) return;
    let current = parseInt(input.value) || 10;
    current += amount;
    if (current > 10000) current = 10000;
    input.value = current;
}

function getBet(game) {
    const input = document.getElementById(game + '-bet');
    if (!input) return 10;
    let val = parseInt(input.value) || 10;
    if (val < 10) val = 10;
    if (val > 10000) val = 10000;
    return val;
}

// МИНЫ
let minesActive = false, minesCoef = 1.0, minesSafeOpened = 0;
let selectedMines = 1; // По умолчанию 1 мина

const board = document.getElementById('mines-board');
for (let i = 0; i < 16; i++) {
    const cell = document.createElement('div');
    cell.className = 'cell'; cell.dataset.index = i;
    cell.onclick = () => clickMineCell(cell);
    board.appendChild(cell);
}

function selectMines(count, element) {
    if (minesActive) return;
    selectedMines = count;
    document.querySelectorAll('.mine-btn').forEach(btn => btn.classList.remove('active'));
    element.classList.add('active');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
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
    while(window.mineIndices.length < selectedMines) {
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
                if(!c.classList.contains('flipped')) { c.classList.add('flipped', 'bad'); c.innerText = '💣'; }
            });
            document.body.classList.add('shake');
            setTimeout(() => document.body.classList.remove('shake'), 500);
            document.getElementById('mines-btn').innerText = `Играть снова`;
            document.getElementById('mines-btn').style.background = '#007aff';
        } else {
            cell.classList.add('good'); cell.innerText = '💎';
            minesSafeOpened++;

            // НОВАЯ ЖЕСТКАЯ МАТЕМАТИКА (Логарифмический рост)
            // Чем больше открыто, тем меньше прибавка
            let base = 1.0;
            if (selectedMines === 1) base = 1.10;
            if (selectedMines === 2) base = 1.25;
            if (selectedMines === 3) base = 1.45;

            // Формула: базовый кэф * (1 + логарифм от открытых клеток)
            // Это не дает кэфу улететь в космос
            let multiplier = 1 + (Math.log2(minesSafeOpened) * 0.5);
            minesCoef = base * multiplier;

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

loadUserData();