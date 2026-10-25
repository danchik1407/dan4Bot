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

// ================= ЗВУКИ =================
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function playSound(type) {
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain); gain.connect(audioCtx.destination);
    const now = audioCtx.currentTime;

    if (type === 'click') {
        osc.type = 'sine'; osc.frequency.setValueAtTime(1200, now); osc.frequency.exponentialRampToValueAtTime(800, now + 0.05);
        gain.gain.setValueAtTime(0.1, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.05);
        osc.start(now); osc.stop(now + 0.05);
    } else if (type === 'win') {
        osc.type = 'triangle'; osc.frequency.setValueAtTime(523.25, now); osc.frequency.setValueAtTime(659.25, now + 0.1); osc.frequency.setValueAtTime(783.99, now + 0.2);
        gain.gain.setValueAtTime(0.15, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
        osc.start(now); osc.stop(now + 0.4);
    } else if (type === 'lose') {
        osc.type = 'sawtooth'; osc.frequency.setValueAtTime(300, now); osc.frequency.exponentialRampToValueAtTime(100, now + 0.4);
        gain.gain.setValueAtTime(0.15, now); gain.gain.exponentialRampToValueAtTime(0.01, now + 0.4);
        osc.start(now); osc.stop(now + 0.4);
    } else if (type === 'boom') {
        const bufferSize = audioCtx.sampleRate * 0.5;
        const buffer = audioCtx.createBuffer(1, bufferSize, audioCtx.sampleRate);
        const data = buffer.getChannelData(0);
        for (let i = 0; i < bufferSize; i++) data[i] = Math.random() * 2 - 1;
        const noise = audioCtx.createBufferSource(); noise.buffer = buffer;
        const noiseGain = audioCtx.createGain(); noiseGain.gain.setValueAtTime(0.2, now); noiseGain.gain.exponentialRampToValueAtTime(0.01, now + 0.5);
        noise.connect(noiseGain); noiseGain.connect(audioCtx.destination); noise.start(now);
    }
}

// ================= ХОККЕЙ =================
async function placeHockeyBet() {
    if (isProcessing) return;
    if (hasBetThisRound) return tg.showAlert("Вы уже сделали ставку в этом раунде!");
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
        balance = data.new_balance; updateUI(); hasBetThisRound = true; playSound('click');
        document.getElementById('hockey-controls').style.display = 'none';
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

// ФИЗИКА ШАРИКА: РЕЗКИЙ СТАРТ, ПЛАВНОЕ ТОРМОЖЕНИЕ, ЗУМ ЗА 4 СЕК
function startBallPhysics() {
    if (ball.active || ball.hasStartedAnim) return;
    ball.hasStartedAnim = true;
    ball.x = 50; ball.y = 50;

    // Резкий разгон (большая начальная скорость)
    let angle = Math.random() * Math.PI * 2;
    let speed = 8.0;
    ball.vx = Math.cos(angle) * speed;
    ball.vy = Math.sin(angle) * speed;
    ball.active = true;

    const arena = document.getElementById('hockey-arena');
    const ballEl = document.getElementById('hockey-ball');

    arena.style.transform = 'scale(1)';
    arena.style.transformOrigin = 'center center';

    let scale = 1;
    const maxScale = 2.5;
    const startTime = performance.now();
    const duration = 15000; // Всего 15 секунд полета
    const zoomStartTime = duration - 4000; // Зум начинается за 4 секунды до конца

    const step = () => {
        if (!ball.active) return;
        const elapsed = performance.now() - startTime;

        ball.x += ball.vx;
        ball.y += ball.vy;

        // Отскоки
        if (ball.x <= 0 || ball.x >= 100) {
            ball.vx *= -1;
            ball.x = Math.max(0, Math.min(100, ball.x));
            playSound('click');
        }
        if (ball.y <= 0 || ball.y >= 100) {
            ball.vy *= -1;
            ball.y = Math.max(0, Math.min(100, ball.y));
            playSound('click');
        }

        // ПЛАВНОЕ ЗАМЕДЛЕНИЕ
        ball.vx *= 0.985;
        ball.vy *= 0.985;

        if (ballEl) {
            ballEl.style.left = ball.x + '%';
            ballEl.style.top = ball.y + '%';
        }

        // ЗУМ ЗА 4 СЕКУНДЫ ДО ОСТАНОВКИ
        if (elapsed > zoomStartTime) {
            scale += (maxScale - scale) * 0.02;
            arena.style.transform = `scale(${scale})`;
            arena.style.transformOrigin = `${ball.x}% ${ball.y}%`;
        }

        // Остановка
        if (Math.abs(ball.vx) < 0.05 && Math.abs(ball.vy) < 0.05) {
            ball.active = false;
            fetch(`${API_URL}/api/finish_hockey`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ initData: tg.initData, x: ball.x, y: ball.y })
            });
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

            // ЗАДЕРЖКА 1 СЕК ПЕРЕД ПОКАЗОМ МОДАЛКИ
            const modal = document.getElementById('winner-modal');
            if (state.winner && !hasShownWinner) {
                hasShownWinner = true;
                setTimeout(() => {
                    document.getElementById('winner-modal-name').innerText = state.winner.name;
                    document.getElementById('winner-modal-amount').innerText = `+${state.bank} 🪙`;
                    modal.classList.add('active');
                    if (state.winner.id === String(user.id)) { playSound('win'); setTimeout(async () => { await loadUserData(); }, 1000); } else { playSound('lose'); }
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

            sector.style.width = width + '%';
            sector.style.background = p.color;
            sector.style.borderRight = '1px solid rgba(0,0,0,0.5)';
            sector.style.overflow = 'hidden';

            const shapeClass = 'shape-circle';

            const shapeWrap = document.createElement('div'); shapeWrap.className = 'player-shape-wrapper';
            const shape = document.createElement('div'); shape.className = `player-shape ${shapeClass}`;

            const sectorPixelWidth = (width / 100) * arena.offsetWidth;
            let size = Math.min(sectorPixelWidth * 0.7, 60);
            if (size < 25) size = 25;

            shape.style.width = size + 'px';
            shape.style.height = size + 'px';

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

// ================= СУНДУКИ =================
let pendingChestReward = 0, pendingChestIsKey = false, pendingChestIsGold = false;
async function openChest(type) {
    if (isProcessing) return;
    const isGold = type === 'gold';
    if (isGold && keys <= 0) return tg.showAlert("У вас нет ключей!");
    isProcessing = true;
    const res = await fetch(`${API_URL}/api/open_chest`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) });
    isProcessing = false;
    if (!isGold && !res.ok) { const err = await res.json(); return tg.showAlert(err.detail || "Сундук перезаряжается!"); }
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
    if (pendingChestIsGold) await changeKeys(-1); if (pendingChestIsKey) await changeKeys(1); else if (pendingChestReward > 0) await changeBalance(pendingChestReward);
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

// ================= ОБЩИЕ =================
async function loadUserData() { try { const response = await fetch(`${API_URL}/api/user`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData }) }); const data = await response.json(); balance = data.coins; keys = data.keys || 0; inventory = data.inventory || []; lastChestTime = data.last_chest || 0; updateUI(); checkChestCooldown(); } catch (e) { } }
async function changeBalance(amount) { const response = await fetch(`${API_URL}/api/update_balance`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) }); const data = await response.json(); balance = data.new_balance; updateUI(); }
async function changeKeys(amount) { const response = await fetch(`${API_URL}/api/update_keys`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ initData: tg.initData, amount: amount }) }); const data = await response.json(); keys = data.new_keys; updateUI(); }
async function loadGlobalTop() { const list = document.getElementById('global-top-list'); list.innerHTML = '<p style="text-align: center; color: #888;">Загрузка...</p>'; try { const res = await fetch(`${API_URL}/api/global_top`); const users = await res.json(); if (!users || users.length === 0) { list.innerHTML = '<p style="text-align: center; color: #888;">Топ пока пуст</p>'; return; } list.innerHTML = ''; const medals = ["🥇", "🥈", "🥉"]; users.forEach((u, i) => { const prefix = i < 3 ? medals[i] : `<span class="rank">${i+1}</span>`; const div = document.createElement('div'); div.innerHTML = `<div style="display: flex; align-items: center; gap: 10px;">${prefix} <span>${u.username || 'Игрок'}</span></div><span>${u.coins} 💎</span>`; list.appendChild(div); }); } catch (e) { list.innerHTML = '<p style="text-align: center; color: #888;">Ошибка загрузки</p>'; } }
function updateUI() {
    document.getElementById('user-balance').innerText = balance;
    document.getElementById('profile-coins').innerText = balance;
    document.getElementById('profile-keys').innerText = keys;

    document.querySelectorAll('.game-balance-val').forEach(el => {
        el.innerText = balance;
    });

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
function checkChestCooldown() { const now = Math.floor(Date.now() / 1000); const diff = now - lastChestTime; const btn = document.getElementById('normal-chest-btn'); if (diff < 18000) { btn.disabled = true; btn.innerText = `КД: ${Math.floor((18000 - diff) / 3600)}ч ${Math.floor(((18000 - diff) % 3600) / 60)}м`; setTimeout(checkChestCooldown, 60000); } else { btn.disabled = false; btn.innerText = 'Открыть (Бесплатно)'; } }
function showWin(text) { const popup = document.getElementById('win-popup'); popup.querySelector('.win-text').innerText = text; popup.classList.add('show'); setTimeout(() => popup.classList.remove('show'), 1500); }
function switchTab(tabId, element) { const popup = document.getElementById('win-popup'); if (popup) popup.classList.remove('show'); document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active')); document.getElementById('tab-' + tabId).classList.add('active'); document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active')); if (element) element.classList.add('active'); if (tabId === 'top') loadGlobalTop(); }
function closeGame() { stopHockeyPolling(); const popup = document.getElementById('win-popup'); if (popup) popup.classList.remove('show'); document.querySelectorAll('.game-screen').forEach(s => s.classList.remove('active')); document.getElementById('main-header').style.display = 'flex'; document.getElementById('main-nav').style.display = 'flex'; switchTab('play', document.querySelectorAll('.nav-item')[0]); }
function openGame(game) { document.getElementById('main-header').style.display = 'none'; document.getElementById('main-nav').style.display = 'none'; document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); document.getElementById('screen-' + game).classList.add('active'); if (game === 'hockey') { startHockeyPolling(); } if (game === 'crash') { initCrashField(); } }
function addBet(game, amount) { const input = document.getElementById(game + '-bet'); if (!input) return; let current = parseInt(input.value) || 10; current += amount; if (current > 10000) current = 10000; input.value = current; }
function getBet(game) { const input = document.getElementById(game + '-bet'); if (!input) return 10; let val = parseInt(input.value) || 10; if (val < 10) val = 10; if (val > 10000) val = 10000; return val; }

// ================= МИНЫ =================
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
    window.mineIndices = []; while(window.mineIndices.length < selectedMines) { let r = Math.floor(Math.random() * 16); if(window.mineIndices.indexOf(r) === -1) window.mineIndices.push(r); }
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
            let base = 1.0; if (selectedMines === 1) base = 1.05; if (selectedMines === 2) base = 1.15; if (selectedMines === 3) base = 1.30;
            let multiplier = 1 + (Math.log2(minesSafeOpened) * 0.3); minesCoef = base * multiplier;
            const cashout = Math.floor(currentBet * minesCoef);
            document.getElementById('mines-current-coef').innerText = `x${minesCoef.toFixed(2)}`; document.getElementById('mines-cashout-amount').innerText = cashout; document.getElementById('mines-btn').innerText = `Забрать (${cashout} 🪙)`;
        }
        isProcessing = false;
    }, 200);
}

// ================= КАПИБАРА (CRASH) =================
const SVG_CAPYBARA = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" shape-rendering="crispEdges"><path fill="#5c3a21" d="M4 6h2v1H4zM6 5h2v1H6zM8 4h4v1H8zM12 4h3v1h-3zM15 5h2v1h-2zM17 6h1v6h-1zM16 12h1v2h-1zM14 14h2v2h-2zM11 14h2v2h-2zM8 14h2v2H8zM5 14h2v2H5zM3 12h1v2H3zM2 6h1v6H2zM3 11h1v1H3zM4 9h1v1H4zM6 9h1v1H6zM9 8h1v1H9zM12 9h1v1h-1zM14 8h1v1h-1zM16 8h1v1h-1z"/><path fill="#000" d="M13 7h1v2h-1zM14 6h1v1h-1z"/><path fill="#fff" d="M13 7h1v1h-1z"/></svg>`;
const SVG_CAR = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 40" shape-rendering="crispEdges"><path fill="#222" d="M4 2h12v36H4z"/><path fill="#444" d="M5 3h10v34H5z"/><path fill="#111" d="M6 4h8v8H6zM6 20h8v8H6z"/><path fill="#ff0" d="M6 37h2v1H6zM12 37h2v1h-2z"/><path fill="#111" d="M4 10h1v2H4zM15 10h1v2h-1z"/></svg>`;
const SVG_BARRIER = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" shape-rendering="crispEdges"><path fill="#ffcc00" d="M2 5h16v2H2zM2 13h16v2H2zM3 7h2v6H3zM15 7h2v6h-2zM5 7h2v1H5zM8 7h2v1H8zM11 7h2v1h-2zM14 7h1v1h-1z"/></svg>`;

const imgCapybara = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(SVG_CAPYBARA)}`;
const imgCar = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(SVG_CAR)}`;
const imgBarrier = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(SVG_BARRIER)}`;

const DIFFICULTIES = {
    easy: { multipliers: [1.1, 1.2, 1.35, 1.5, 1.7, 1.9, 2.1, 2.35, 2.6, 2.9, 3.2, 3.5, 3.8, 4.1, 4.5], crashBase: 0.1 },
    medium: { multipliers: [1.2, 1.5, 1.9, 2.4, 3.0, 3.8, 4.8, 6.0, 7.5, 9.0, 11.0, 13.0, 15.0, 18.0, 22.0], crashBase: 0.3 },
    hard: { multipliers: [1.5, 2.0, 2.8, 3.8, 5.0, 6.5, 8.0, 10.0, 12.0, 15.0, 18.0, 22.0, 26.0, 30.0, 35.0], crashBase: 0.45 },
    hardcore: { multipliers: [2.0, 3.0, 4.5, 6.0, 8.0, 10.0, 13.0, 16.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.0], crashBase: 0.6 }
};

let crashState = {
    active: false,
    currentStep: 0,
    currentCoef: 1.0,
    bet: 0,
    difficulty: 'easy',
    field: [],
    capyPos: { row: 4, col: 1 },
    isMoving: false
};

function setCrashDiff(diff, btn) {
    if (crashState.active) return;
    crashState.difficulty = diff;
    document.querySelectorAll('.crash-diff-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    playSound('click');
}

function initCrashField() {
    const field = document.getElementById('crash-field');
    field.innerHTML = '';
    crashState.field = [];

    for (let r = 0; r < 5; r++) {
        let rowArr = [];
        for (let c = 0; c < 4; c++) {
            const cell = document.createElement('div');
            cell.className = 'crash-cell';
            cell.dataset.row = r;
            cell.dataset.col = c;
            field.appendChild(cell);
            rowArr.push(cell);
        }
        crashState.field.push(rowArr);
    }

    crashState.capyPos = { row: 4, col: 1 };
    updateCapyPosition();
}

function updateCapyPosition() {
    const capy = document.getElementById('crash-capybara');
    const cell = crashState.field[crashState.capyPos.row][crashState.capyPos.col];
    capy.style.left = (cell.offsetLeft + cell.offsetWidth / 2 - 20) + 'px';
    capy.style.top = (cell.offsetTop + cell.offsetHeight / 2 - 20) + 'px';
}

async function crashAction() {
    if (isProcessing) return;

    const btn = document.getElementById('crash-btn');
    const betContainer = document.getElementById('crash-bet-container');

    if (!crashState.active) {
        const bet = parseInt(document.getElementById('crash-bet').value) || 10;
        if (balance < bet) return tg.showAlert("Недостаточно коинов!");

        isProcessing = true;
        await changeBalance(-bet);
        isProcessing = false;

        crashState.active = true;
        crashState.bet = bet;
        crashState.currentStep = 0;
        crashState.currentCoef = 1.0;

        betContainer.style.display = 'none';
        btn.innerText = 'Сделать шаг';
        btn.style.background = '#007aff';

        document.querySelectorAll('.crash-cell').forEach(c => {
            c.innerHTML = '';
            c.classList.remove('safe', 'crash');
        });

        updateCrashUI();
        return;
    }

    if (crashState.isMoving) return;
    if (crashState.currentStep >= 15) return tg.showAlert("Ты прошел все 15 шагов! Забирай выигрыш.");

    crashState.isMoving = true;
    const config = DIFFICULTIES[crashState.difficulty];
    let crashChance = config.crashBase + (crashState.currentStep * 0.03);
    if (crashChance > 0.95) crashChance = 0.95;

    const isCrash = Math.random() < crashChance;

    if (isCrash) {
        playSound('boom');
        const cell = crashState.field[crashState.capyPos.row][crashState.capyPos.col];
        cell.innerHTML = `<img src="${imgCar}" style="width:100%; height:100%; object-fit:contain;">`;
        cell.classList.add('crash');

        setTimeout(() => {
            const capy = document.getElementById('crash-capybara');
            capy.style.backgroundImage = 'none';
            capy.style.background = '#5c3a21';
            capy.style.borderRadius = '0';
            capy.style.width = '20px';
            capy.style.height = '20px';
            capy.style.opacity = '0.5';

            setTimeout(resetCrashGame, 2000);
        }, 500);

    } else {
        playSound('click');
        crashState.currentStep++;
        crashState.currentCoef = config.multipliers[crashState.currentStep - 1];

        const cell = crashState.field[crashState.capyPos.row][crashState.capyPos.col];
        cell.innerHTML = `<img src="${imgBarrier}" style="width:100%; height:100%; object-fit:contain;">`;
        cell.classList.add('safe');

        let nextRow = crashState.capyPos.row;
        let nextCol = crashState.capyPos.col + 1;
        if (nextCol > 3) { nextCol = 0; nextRow--; }

        if (nextRow < 0) {
            crashState.currentStep = 15;
            crashState.currentCoef = config.multipliers[14];
        } else {
            crashState.capyPos = { row: nextRow, col: nextCol };
            updateCapyPosition();
        }

        updateCrashUI();
    }

    crashState.isMoving = false;
}

function updateCrashUI() {
    const btn = document.getElementById('crash-btn');
    const winAmount = Math.floor(crashState.bet * crashState.currentCoef);

    document.getElementById('crash-coef').innerText = `x${crashState.currentCoef.toFixed(2)}`;
    document.getElementById('crash-win').innerText = winAmount;

    if (crashState.currentStep > 0) {
        btn.innerText = `Забрать ${winAmount} 🪙`;
        btn.style.background = '#34c759';
    }
}

function resetCrashGame() {
    crashState.active = false;
    crashState.currentStep = 0;
    crashState.currentCoef = 1.0;

    const capy = document.getElementById('crash-capybara');
    capy.style.opacity = '1';
    capy.style.backgroundImage = `url('${imgCapybara}')`;
    capy.style.background = 'transparent';
    capy.style.width = '40px';
    capy.style.height = '40px';
    capy.style.borderRadius = '50%';

    document.getElementById('crash-bet-container').style.display = 'block';
    const btn = document.getElementById('crash-btn');
    btn.innerText = 'Начать игру';
    btn.style.background = '#007aff';

    document.getElementById('crash-coef').innerText = 'x1.0';
    document.getElementById('crash-win').innerText = '0';

    document.querySelectorAll('.crash-cell').forEach(c => {
        c.innerHTML = '';
        c.classList.remove('safe', 'crash');
    });

    crashState.capyPos = { row: 4, col: 1 };
    updateCapyPosition();
}

loadUserData();