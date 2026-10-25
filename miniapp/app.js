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
let balance = 0, keys = 0, currentBet = 10, inventory = [], lastChestTime = 0, isProcessing = false;
let hockeyInterval = null, hasBetThisRound = false, hasShownWinner = false;
let ball = { x: 50, y: 50, vx: 0, vy: 0, active: false, animationId: null, hasStartedAnim: false };

// ЗВУКИ (сокращены для экономии места, они у тебя были)
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function playSound(type) { /* ... твой код звуков ... */ }

// ХОККЕЙ
async function placeHockeyBet() {
    if (isProcessing) return;
    if (hasBetThisRound) return tg.showAlert("Вы уже сделали ставку!");
    const bet = parseInt(document.getElementById('hockey-bet').value) || 10;
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");

    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/place_bet`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, amount: bet })
        });
        if (!res.ok) { isProcessing = false; return tg.showAlert("Ставки закрыты!"); }
        const data = await res.json();
        balance = data.new_balance; updateUI(); hasBetThisRound = true;
        document.getElementById('hockey-controls').style.display = 'none';
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

function startBallPhysics() {
    if (ball.active || ball.hasStartedAnim) return;
    ball.hasStartedAnim = true;
    ball.x = 50; ball.y = 50;
    let dir = Math.random() > 0.5 ? 1 : -1; let speed = 2.0; ball.active = true;
    const arena = document.getElementById('hockey-arena'); const ballEl = document.getElementById('hockey-ball');
    arena.style.transform = 'scale(1)'; arena.style.transformOrigin = 'center center';
    let scale = 1; const maxScale = 2.5; const startTime = performance.now(); const duration = 6000;

    const step = () => {
        if (!ball.active) return;
        const elapsed = performance.now() - startTime;
        ball.x += speed * dir;
        if (ball.x >= 100) { ball.x = 100; dir = -1; speed *= 0.9; }
        else if (ball.x <= 0) { ball.x = 0; dir = 1; speed *= 0.9; }
        if (elapsed > duration * 0.6) speed *= 0.95;
        if (ballEl) { ballEl.style.left = ball.x + '%'; ballEl.style.top = '50%'; }
        if (elapsed > duration * 0.7) { scale += (maxScale - scale) * 0.05; arena.style.transform = `scale(${scale})`; arena.style.transformOrigin = `${ball.x}% 50%`; }
        if (elapsed >= duration) {
            ball.active = false;
            fetch(`${API_URL}/api/finish_hockey`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, x: ball.x, y: 50 }) });
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
            statusEl.innerText = 'Идёт бросок...'; statusEl.style.color = '#007aff';
            document.getElementById('hockey-controls').style.display = 'none';
            if (!ball.hasStartedAnim) startBallPhysics();
        } else if (state.status === 'finished') {
            statusEl.innerText = 'Раунд окончен!'; statusEl.style.color = '#ff3b30';
            ball.active = false;
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
                document.getElementById('winner-modal-name').innerText = state.winner.name;
                document.getElementById('winner-modal-amount').innerText = `+${state.bank} 🪙`;
                modal.classList.add('active');
                if (state.winner.id === String(user.id)) { playSound('win'); setTimeout(async () => { await loadUserData(); }, 1000); }
                else { playSound('lose'); }
            }
        }
        bankEl.innerText = `Банк: ${state.bank} 🪙`;

        // РЕНДЕР (ВСЕГДА РИСУЕМ)
        arena.querySelectorAll('.hockey-sector').forEach(el => el.remove());
        let totalWidth = 0;
        state.players.forEach((p, index) => {
            const sector = document.createElement('div');
            sector.className = 'hockey-sector';
            let width = p.percent;
            if (index === state.players.length - 1) { width = 100 - totalWidth; } else { width = parseFloat(p.percent.toFixed(2)); totalWidth += width; }
            sector.style.width = width + '%'; sector.style.background = p.color; sector.style.borderRight = '1px solid rgba(0,0,0,0.5)';
            let shapeClass = 'shape-square'; if (index % 4 === 0) shapeClass = 'shape-circle'; if (index % 4 === 1) shapeClass = 'shape-square'; if (index % 4 === 2) shapeClass = 'shape-trapezoid'; if (index % 4 === 3) shapeClass = 'shape-triangle';
            const shapeWrap = document.createElement('div'); shapeWrap.className = 'player-shape-wrapper';
            const shape = document.createElement('div'); shape.className = `player-shape ${shapeClass}`;
            const size = Math.max(40, Math.min(60, width * 4)); shape.style.width = size + 'px'; shape.style.height = size + 'px';
            if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) { shape.innerHTML = `<img src="${p.avatar}" style="width:100%; height:100%; object-fit:cover; border-radius:50%;">`; shape.style.background = 'transparent'; shape.style.border = '2px solid #fff'; }
            else { const letter = p.name ? p.name[0].toUpperCase() : '?'; shape.innerHTML = `<span style="color:white; font-weight:900; font-size:${size/2}px; text-shadow: 0 2px 4px rgba(0,0,0,0.5);">${letter}</span>`; shape.style.backgroundColor = p.color; shape.style.border = '2px solid rgba(255,255,255,0.5)'; }
            shapeWrap.appendChild(shape); sector.appendChild(shapeWrap); arena.appendChild(sector);
        });
        if (!document.getElementById('hockey-ball')) { const ballEl = document.createElement('div'); ballEl.className = 'hockey-ball'; ballEl.id = 'hockey-ball'; arena.appendChild(ballEl); }

        const list = document.getElementById('hockey-participants');
        list.innerHTML = '';
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

// ИСТОРИЯ
async function openHistory() {
    const modal = document.getElementById('history-modal'); const list = document.getElementById('history-list');
    list.innerHTML = '<p style="text-align:center;color:#888;">Загрузка...</p>'; modal.classList.add('active');
    try {
        const res = await fetch(`${API_URL}/api/hockey_history`); const history = await res.json(); list.innerHTML = '';
        if (history.length === 0) { list.innerHTML = '<p style="text-align:center;color:#888;">История пуста</p>'; return; }
        history.forEach(item => {
            const div = document.createElement('div'); div.style.cssText = "background: #252525; padding: 15px; border-radius: 10px; margin-bottom: 10px; border: 1px solid #333;";
            let playersText = item.players.map(p => `${p.name} (${p.bet} 🪙) ${p.is_winner ? '🏆' : ''}`).join('\n');
            div.innerHTML = `<div style="display:flex; justify-content:space-between; width:100%; align-items:center; margin-bottom: 10px;"><span style="font-weight:bold; color: #ffcc00;">🏆 ${item.winner_name}</span><span style="color:#34c759; font-weight:900; font-size: 18px;">+${item.amount} 🪙</span></div><div style="font-size: 12px; color: #8e8e93; white-space: pre-line;">${playersText}</div>`;
            list.appendChild(div);
        });
    } catch (e) { list.innerHTML = '<p style="text-align:center;color:#888;">Ошибка загрузки истории</p>'; }
}

// ОБЩИЕ
async function loadUserData() { try { const response = await fetch(`${API_URL}/api/user`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) }); const data = await response.json(); balance = data.coins; keys = data.keys || 0; inventory = data.inventory || []; lastChestTime = data.last_chest || 0; updateUI(); checkChestCooldown(); } catch (e) { } }
async function changeBalance(amount) { const response = await fetch(`${API_URL}/api/update_balance`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) }); const data = await response.json(); balance = data.new_balance; updateUI(); }
async function changeKeys(amount) { const response = await fetch(`${API_URL}/api/update_keys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) }); const data = await response.json(); keys = data.new_keys; updateUI(); }
async function loadGlobalTop() { const list = document.getElementById('global-top-list'); list.innerHTML = '<p style="text-align: center; color: #888;">Загрузка...</p>'; try { const res = await fetch(`${API_URL}/api/global_top`); const users = await res.json(); if (!users || users.length === 0) { list.innerHTML = '<p style="text-align: center; color: #888;">Топ пока пуст</p>'; return; } list.innerHTML = ''; const medals = ["🥇", "🥈", "🥉"]; users.forEach((u, i) => { const prefix = i < 3 ? medals[i] : `<span class="rank">${i+1}</span>`; const div = document.createElement('div'); div.innerHTML = `<div style="display: flex; align-items: center; gap: 10px;">${prefix} <span>${u.username || 'Игрок'}</span></div><span>${u.coins} 💎</span>`; list.appendChild(div); }); } catch (e) { list.innerHTML = '<p style="text-align: center; color: #888;">Ошибка загрузки</p>'; } }
function updateUI() { document.getElementById('user-balance').innerText = balance; document.getElementById('profile-coins').innerText = balance; document.getElementById('profile-keys').innerText = keys; const inv = document.getElementById('inventory'); let capCount = 0, uniqCount = 0; inventory.forEach(item => { if (item === 'capsule') capCount++; if (item === 'uniq_capsule') uniqCount++; }); if (capCount === 0 && uniqCount === 0) { inv.innerHTML = `<div class="inv-item">📦<br><span>Пусто</span></div>`; } else { let html = ''; if (capCount > 0) html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">💊<br><span>Капсула x${capCount}</span></div>`; if (uniqCount > 0) html += `<div class="inv-item" onclick="tg.showAlert('Скоро...')">⚱️<br><span>Уник. Капсула x${uniqCount}</span></div>`; inv.innerHTML = html; } }
function checkChestCooldown() { const now = Math.floor(Date.now() / 1000); const diff = now - lastChestTime; const btn = document.getElementById('normal-chest-btn'); if (diff < 18000) { btn.disabled = true; btn.innerText = `КД: ${Math.floor((18000 - diff) / 3600)}ч ${Math.floor(((18000 - diff) % 3600) / 60)}м`; setTimeout(checkChestCooldown, 60000); } else { btn.disabled = false; btn.innerText = 'Открыть (Бесплатно)'; } }

// СУНДУКИ (сокращены)
let pendingChestReward = 0, pendingChestIsKey = false, pendingChestIsGold = false;
async function openChest(type) { /* ... код сундуков ... */ }
async function claimChestReward() { /* ... код сундуков ... */ }
async function buyItem(itemId, price) { /* ... код магазина ... */ }
function showWin(text) { const popup = document.getElementById('win-popup'); popup.querySelector('.win-text').innerText = text; popup.classList.add('show'); setTimeout(() => popup.classList.remove('show'), 1500); }
function switchTab(tabId, element) { const popup = document.getElementById('win-popup'); if (popup) popup.classList.remove('show'); document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active')); document.getElementById('tab-' + tabId).classList.add('active'); document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active')); if (element) element.classList.add('active'); if (tabId === 'top') loadGlobalTop(); }
function closeGame() { stopHockeyPolling(); const popup = document.getElementById('win-popup'); if (popup) popup.classList.remove('show'); document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active')); document.getElementById('main-header').style.display = 'flex'; document.getElementById('main-nav').style.display = 'flex'; switchTab('play', document.querySelectorAll('.nav-item')[0]); }
function openGame(game) { document.getElementById('main-header').style.display = 'none'; document.getElementById('main-nav').style.display = 'none'; document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); document.getElementById('screen-' + game).classList.add('active'); if (game === 'hockey') { startHockeyPolling(); } }
function addBet(game, amount) { const input = document.getElementById(game + '-bet'); if (!input) return; let current = parseInt(input.value) || 10; current += amount; if (current > 10000) current = 10000; input.value = current; }
function getBet(game) { const input = document.getElementById(game + '-bet'); if (!input) return 10; let val = parseInt(input.value) || 10; if (val < 10) val = 10; if (val > 10000) val = 10000; return val; }

// МИНЫ (ФИКС КНОПКИ)
let minesActive = false, minesCoef = 1.0, minesSafeOpened = 0, selectedMines = 1;
const board = document.getElementById('mines-board');
for (let i = 0; i < 16; i++) { const cell = document.createElement('div'); cell.className = 'cell'; cell.dataset.index = i; cell.onclick = () => clickMineCell(cell); board.appendChild(cell); }
function selectMines(count, element) { if (minesActive) return; selectedMines = count; document.querySelectorAll('.mine-btn').forEach(btn => btn.classList.remove('active')); element.classList.add('active'); }
async function minesAction() {
    if (isProcessing) return;
    if (minesActive) {
        isProcessing = true; minesActive = false;
        const cashout = Math.floor(currentBet * minesCoef);
        const btn = document.getElementById('mines-btn');
        btn.disabled = true; btn.innerText = 'Забрано!'; btn.style.background = '#555';
        await changeBalance(cashout); showWin(cashout);
        document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
        // ФИКС: Кнопка возвращается в исходное состояние
        btn.disabled = false;
        btn.innerText = 'Сделать ставку';
        btn.style.background = '#007aff';
        isProcessing = false; return;
    }
    currentBet = getBet('mines');
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");
    isProcessing = true; await changeBalance(-currentBet); isProcessing = false;
    minesActive = true; minesCoef = 1.0; minesSafeOpened = 0;
    document.querySelectorAll('.cell').forEach(c => { c.className = 'cell'; c.innerText = ''; c.style.pointerEvents = 'auto'; });
    document.getElementById('mines-btn').innerText = 'Забрать (0 🪙)'; document.getElementById('mines-btn').style.background = '#34c759';
    document.getElementById('mines-current-coef').innerText = 'x1.00'; document.getElementById('mines-cashout-amount').innerText = '0';
    window.mineIndices = []; while(window.mineIndices.length < selectedMines) { let r = Math.floor(Math.random() * 16); if(window.mineIndices.indexOf(r) === -1) window.mineIndices.push(r); }
}
function clickMineCell(cell) {
    if (!minesActive || isProcessing) return;
    const idx = parseInt(cell.dataset.index); if (cell.classList.contains('flipped')) return;
    isProcessing = true; cell.classList.add('flipped');
    setTimeout(() => {
        if (window.mineIndices.includes(idx)) {
            cell.classList.add('bad'); cell.innerText = '💥'; minesActive = false;
            document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
            window.mineIndices.forEach(i => { const c = board.children[i]; if(!c.classList.contains('flipped')) { c.classList.add('flipped', 'bad'); c.innerText = '💣'; } });
            const btn = document.getElementById('mines-btn'); btn.innerText = `Играть снова`; btn.style.background = '#007aff';
        } else {
            cell.classList.add('good'); cell.innerText = '💎'; minesSafeOpened++;
            let base = 1.0; if (selectedMines === 1) base = 1.05; if (selectedMines === 2) base = 1.15; if (selectedMines === 3) base = 1.30;
            let multiplier = 1 + (Math.log2(minesSafeOpened) * 0.3); minesCoef = base * multiplier;
            const cashout = Math.floor(currentBet * minesCoef);
            document.getElementById('mines-current-coef').innerText = `x${minesCoef.toFixed(2)}`; document.getElementById('mines-cashout-amount').innerText = cashout; document.getElementById('mines-btn').innerText = `Забрать (${cashout} 🪙)`;
        }
        isProcessing = false;
    }, 200);
}

loadUserData();