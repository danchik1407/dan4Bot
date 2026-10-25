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

let ball = { x: 0, y: 50, vx: 0, vy: 0, active: false, animationId: null };

// ЗВУКИ
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

// ХОККЕЙ
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
        if (!res.ok) {
            const err = await res.json(); isProcessing = false;
            return tg.showAlert(err.detail || "Ставки закрыты!");
        }
        const data = await res.json();
        balance = data.new_balance; updateUI(); hasBetThisRound = true; playSound('click');
        document.getElementById('hockey-controls').style.display = 'none';
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
}

function startBallPhysics(targetX) {
    if (ball.active) return;

    ball.x = 0;
    ball.y = 50;
    ball.active = true;

    const arena = document.getElementById('hockey-arena');
    const ballEl = document.getElementById('hockey-ball');

    arena.style.transform = 'scale(1)';
    arena.style.transformOrigin = 'center center';

    let scale = 1;
    const maxScale = 2.5;
    let speed = 2.0;

    const step = () => {
        if (!ball.active) return;

        const dist = targetX - ball.x;
        let friction = 0.985;
        if (dist < 15) friction = 0.96;

        speed *= friction;

        if (speed < 0.05 || Math.abs(dist) < 0.1) {
            ball.x = targetX;
            if (ballEl) ballEl.style.left = ball.x + '%';

            arena.style.transformOrigin = `${ball.x}% 50%`;
            arena.style.transform = `scale(${maxScale})`;

            ball.active = false;
            return;
        }

        ball.x += speed;
        if (ballEl) ballEl.style.left = ball.x + '%';

        if (speed < 1.0) {
            scale += (maxScale - scale) * 0.05;
            arena.style.transform = `scale(${scale})`;
            arena.style.transformOrigin = `${ball.x}% 50%`;
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
            hasShownWinner = false;
            ball.active = false;

            if (ball.animationId) cancelAnimationFrame(ball.animationId);

            const ballEl = document.getElementById('hockey-ball');
            if (ballEl) { ballEl.style.left = '0%'; ballEl.style.top = '50%'; }
            if (arena) {
                arena.style.transform = 'scale(1)';
                arena.style.transformOrigin = 'center center';
                arena.querySelectorAll('.hockey-sector').forEach(s => s.style.boxShadow = 'none');
            }
            document.getElementById('winner-modal').classList.remove('active');

        } else if (state.status === 'rolling') {
            statusEl.innerText = 'Идёт бросок...'; statusEl.style.color = '#007aff';
            document.getElementById('hockey-controls').style.display = 'none';
            if (!ball.active) startBallPhysics(state.win_x);

        } else if (state.status === 'finished') {
            statusEl.innerText = 'Раунд окончен!'; statusEl.style.color = '#ff3b30';
            ball.active = false;
            if (ball.animationId) cancelAnimationFrame(ball.animationId);

            if (state.winner) {
                const sectors = arena.querySelectorAll('.hockey-sector');
                const winnerId = state.winner.id;
                const winnerIndex = state.players.findIndex(p => p.id === winnerId);
                if (winnerIndex !== -1 && sectors[winnerIndex]) {
                    sectors[winnerIndex].style.transition = 'box-shadow 0.3s';
                    sectors[winnerIndex].style.boxShadow = 'inset 0 0 50px #fff, 0 0 30px #fff';
                    setTimeout(() => { sectors[winnerIndex].style.boxShadow = 'none'; }, 1000);
                }
            }

            if (arena) {
                arena.style.transform = 'scale(1)';
                arena.style.transformOrigin = 'center center';
            }

            const modal = document.getElementById('winner-modal');
            if (state.winner && !hasShownWinner) {
                hasShownWinner = true;
                document.getElementById('winner-modal-name').innerText = state.winner.name;
                document.getElementById('winner-modal-amount').innerText = `+${state.bank} 🪙`;
                modal.classList.add('active');
                if (state.winner.id === String(user.id)) {
                    playSound('win'); if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
                    setTimeout(async () => { await loadUserData(); }, 1000);
                } else {
                    playSound('lose'); if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
                }
            }
        }

        bankEl.innerText = `Банк: ${state.bank} 🪙`;

        arena.querySelectorAll('.hockey-sector').forEach(el => el.remove());
        let totalWidth = 0;
        state.players.forEach((p, index) => {
            const sector = document.createElement('div');
            sector.className = 'hockey-sector';

            let width = p.percent;
            if (index === state.players.length - 1) {
                width = 100 - totalWidth;
            } else {
                width = parseFloat(p.percent.toFixed(2));
                totalWidth += width;
            }

            sector.style.width = width + '%';
            sector.style.background = p.color;
            sector.style.borderRight = '1px solid rgba(0,0,0,0.5)';

            let shapeClass = 'shape-square';
            if (index % 4 === 0) shapeClass = 'shape-circle';
            if (index % 4 === 1) shapeClass = 'shape-square';
            if (index % 4 === 2) shapeClass = 'shape-trapezoid';
            if (index % 4 === 3) shapeClass = 'shape-triangle';

            if (width > 5) {
                const shapeWrap = document.createElement('div');
                shapeWrap.className = 'player-shape-wrapper';
                const shape = document.createElement('div');
                shape.className = `player-shape ${shapeClass}`;

                const size = Math.max(30, Math.min(60, width * 4));
                shape.style.width = size + 'px';
                shape.style.height = size + 'px';

                // ЛОГИКА АВАТАРОК: Если есть ссылка на фото - показываем, иначе букву
                if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) {
                    shape.innerHTML = `<img src="${p.avatar}" style="width:100%; height:100%; object-fit:cover;">`;
                    shape.style.background = 'transparent';
                    shape.style.border = '2px solid #fff';
                } else {
                    // Для ботов и юзеров без фото — цветной кружок с буквой
                    const letter = p.name ? p.name[0].toUpperCase() : '?';
                    shape.innerHTML = `<span style="color:white; font-weight:900; font-size:${size/2}px; text-shadow: 0 2px 4px rgba(0,0,0,0.5);">${letter}</span>`;
                    shape.style.backgroundColor = p.color; // Используем цвет сектора или свой
                    shape.style.border = '2px solid rgba(255,255,255,0.5)';
                }
                shapeWrap.appendChild(shape);
                sector.appendChild(shapeWrap);
            }
            arena.appendChild(sector);
        });

        if (!document.getElementById('hockey-ball')) {
            const ballEl = document.createElement('div'); ballEl.className = 'hockey-ball'; ballEl.id = 'hockey-ball'; arena.appendChild(ballEl);
        }

        const list = document.getElementById('hockey-participants');
        list.innerHTML = '';
        state.players.forEach((p, i) => {
            const div = document.createElement('div'); div.className = 'part-item';
            const width = p.percent || (p.bet / state.bank * 100);
            let iconHtml = '';

            // ЛОГИКА ИКОНОК В СПИСКЕ
            if (!p.is_bot && p.avatar && p.avatar.startsWith('http')) {
                iconHtml = `<img src="${p.avatar}" style="width:20px; height:20px; border-radius:50%; margin-right:5px; vertical-align:middle;">`;
            } else {
                const letter = p.name ? p.name[0].toUpperCase() : '?';
                // Фон иконки берем из цвета игрока
                iconHtml = `<span style="display:inline-flex; justify-content:center; align-items:center; width:20px; height:20px; border-radius:50%; background:${p.color}; color:white; font-size:10px; font-weight:bold; margin-right:5px;">${letter}</span>`;
            }
            div.innerHTML = `<div class="name"><span>#${i+1}</span>${iconHtml}<span>${p.name}</span></div><div class="stats">${width.toFixed(1)}% | ${p.bet} 🪙</div>`;
            list.appendChild(div);
        });
    } catch (e) { console.error(e); }
}

function startHockeyPolling() {
    if (hockeyInterval) clearInterval(hockeyInterval);
    pollHockeyState(); hockeyInterval = setInterval(pollHockeyState, 200);
}

function stopHockeyPolling() {
    if (hockeyInterval) clearInterval(hockeyInterval);
    hockeyInterval = null; if (ball.animationId) cancelAnimationFrame(ball.animationId);
}

function closeWinnerModal() { document.getElementById('winner-modal').classList.remove('active'); }

// ИСТОРИЯ
async function loadHistory() {
    try {
        const res = await fetch(`${API_URL}/api/hockey_history`);
        const history = await res.json();
        const list = document.getElementById('history-list');
        list.innerHTML = '';
        if (history.length === 0) {
            list.innerHTML = '<p style="text-align:center;color:#888;">История пуста</p>';
            return;
        }
        history.forEach(item => {
            const div = document.createElement('div');
            div.className = 'history-item';
            div.style.cssText = "background: #252525; padding: 15px; border-radius: 10px; margin-bottom: 10px; cursor: pointer;";
            div.innerHTML = `
                <div style="display:flex; justify-content:space-between; width:100%; align-items:center;">
                    <span style="font-weight:bold;">🏆 ${item.winner_name}</span>
                    <span style="color:#34c759; font-weight:900;">+${item.amount} 🪙</span>
                </div>
            `;
            div.onclick = () => {
                let playersText = item.players.map(p => `${p.name}: ${p.bet} 🪙 ${p.is_winner ? '🏆' : ''}`).join('\n');
                tg.showAlert(`Победитель: ${item.winner_name}\nСумма: ${item.amount} 🪙\n\nУчастники:\n${playersText}`);
            };
            list.appendChild(div);
        });
    } catch (e) { console.error(e); }
}

function openHistory() {
    loadHistory();
    document.getElementById('history-modal').classList.add('active');
}

// ОБЩИЕ ФУНКЦИИ
async function loadUserData() {
    try {
        const response = await fetch(`${API_URL}/api/user`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        const data = await response.json();
        balance = data.coins; keys = data.keys || 0; inventory = data.inventory || []; lastChestTime = data.last_chest || 0;
        updateUI(); checkChestCooldown();
    } catch (e) { document.getElementById('user-balance').innerText = "Ошибка"; }
}

async function changeBalance(amount) {
    const response = await fetch(`${API_URL}/api/update_balance`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData, amount: amount })
    });
    const data = await response.json(); balance = data.new_balance; updateUI();
}

async function changeKeys(amount) {
    const response = await fetch(`${API_URL}/api/update_keys`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData, amount: amount })
    });
    const data = await response.json(); keys = data.new_keys; updateUI();
}

async function loadGlobalTop() {
    const list = document.getElementById('global-top-list');
    list.innerHTML = '<p style="text-align: center; color: #888;">Загрузка...</p>';
    try {
        const res = await fetch(`${API_URL}/api/global_top`);
        const users = await res.json();
        if (!users || users.length === 0) { list.innerHTML = '<p style="text-align: center; color: #888;">Топ пока пуст</p>'; return; }
        list.innerHTML = '';
        const medals = ["🥇", "🥈", "🥉"];
        users.forEach((u, i) => {
            const prefix = i < 3 ? medals[i] : `<span class="rank">${i+1}</span>`;
            const div = document.createElement('div');
            div.innerHTML = `<div style="display: flex; align-items: center; gap: 10px;">${prefix} <span>${u.username || 'Игрок'}</span></div><span>${u.coins} 💎</span>`;
            list.appendChild(div);
        });
    } catch (e) { list.innerHTML = '<p style="text-align: center; color: #888;">Ошибка загрузки</p>'; }
}

function updateUI() {
    document.getElementById('user-balance').innerText = balance;
    document.getElementById('profile-coins').innerText = balance;
    document.getElementById('profile-keys').innerText = keys;
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

function checkChestCooldown() {
    const now = Math.floor(Date.now() / 1000);
    const diff = now - lastChestTime;
    const btn = document.getElementById('normal-chest-btn');
    if (diff < 18000) {
        btn.disabled = true; btn.innerText = `КД: ${Math.floor((18000 - diff) / 3600)}ч ${Math.floor(((18000 - diff) % 3600) / 60)}м`;
        setTimeout(checkChestCooldown, 60000);
    } else {
        btn.disabled = false; btn.innerText = 'Открыть (Бесплатно)';
    }
}

let pendingChestReward = 0, pendingChestIsKey = false, pendingChestIsGold = false;

async function openChest(type) {
    if (isProcessing) return;
    const isGold = type === 'gold';
    if (isGold && keys <= 0) return tg.showAlert("У вас нет ключей!");

    isProcessing = true;
    const res = await fetch(`${API_URL}/api/open_chest`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData })
    });
    isProcessing = false;
    if (!isGold && !res.ok) {
        const err = await res.json();
        return tg.showAlert(err.detail || "Сундук перезаряжается!");
    }

    document.getElementById('chest-buttons').style.display = 'none';
    const roulette = document.getElementById('roulette-container');
    roulette.style.display = 'block';
    const track = document.getElementById('roulette-track');

    track.style.transform = 'translateX(0)';
    track.innerHTML = '';
    void track.offsetWidth;

    let winIndex = Math.floor(Math.random() * 60) + 20;
    let finalAmount = 0, isKey = false;

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

    const itemWidth = 81;
    const windowWidth = 350;
    const centerOffset = (windowWidth / 2) - (itemWidth / 2);
    const targetScroll = (winIndex * itemWidth) - centerOffset;

    let start = null;
    const duration = 6000;

    function step(timestamp) {
        if (!start) start = timestamp;
        const progress = timestamp - start;
        const easeOut = 1 - Math.pow(1 - Math.min(progress / duration, 1), 4);
        const currentScroll = targetScroll * easeOut;
        track.style.transform = `translateX(-${currentScroll}px)`;

        if (progress < duration) {
            requestAnimationFrame(step);
        } else {
            pendingChestReward = finalAmount;
            pendingChestIsKey = isKey;
            pendingChestIsGold = isGold;
            const modal = document.getElementById('chest-win-modal');
            document.getElementById('chest-win-icon').innerText = isKey ? '🔑' : '💎';
            document.getElementById('chest-win-text').innerText = isKey ? 'ЗОЛОТОЙ КЛЮЧ!' : `Вы получили ${finalAmount} 🪙`;
            modal.classList.add('active');
            playSound('win');
            if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
        }
    }
    requestAnimationFrame(step);
}

async function claimChestReward() {
    if (isProcessing) return;
    isProcessing = true;
    document.getElementById('chest-win-modal').classList.remove('active');
    if (pendingChestIsGold) await changeKeys(-1);
    if (pendingChestIsKey) await changeKeys(1);
    else if (pendingChestReward > 0) await changeBalance(pendingChestReward);
    if (!pendingChestIsGold) lastChestTime = Math.floor(Date.now() / 1000);
    document.getElementById('roulette-container').style.display = 'none';
    document.getElementById('chest-buttons').style.display = 'flex';
    checkChestCooldown();
    pendingChestReward = 0; pendingChestIsKey = false; pendingChestIsGold = false;
    isProcessing = false;
}

async function buyItem(itemId, price) {
    if (isProcessing) return;
    if (balance < price) return tg.showAlert("Недостаточно коинов!");
    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/buy_item`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, item_id: itemId, price: price })
        });
        if (!res.ok) {
            const err = await res.json();
            isProcessing = false;
            return tg.showAlert(err.detail || "Ошибка покупки");
        }
        const data = await res.json();
        balance = data.new_balance;
        inventory = data.inventory;
        updateUI();
        showWin("✅ Успешно куплено!");
        playSound('click');
    } catch(e) { tg.showAlert("Ошибка соединения"); }
    isProcessing = false;
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
    if (tabId === 'top') loadGlobalTop();
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
}

function closeGame() {
    stopHockeyPolling();
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
    if (game === 'hockey') {
        startHockeyPolling();
        loadHistory();
    }
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
let minesActive = false, minesCoef = 1.0, minesSafeOpened = 0, selectedMines = 1;
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
    playSound('click');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
}

async function minesAction() {
    if (isProcessing) return;
    if (minesActive) {
        isProcessing = true; minesActive = false;
        const cashout = Math.floor(currentBet * minesCoef);
        const btn = document.getElementById('mines-btn');
        btn.disabled = true; btn.innerText = 'Забрано!'; btn.style.background = '#555';
        await changeBalance(cashout); showWin(cashout); playSound('win');
        document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
        isProcessing = false; return;
    }
    currentBet = getBet('mines');
    if (balance < currentBet) return tg.showAlert("Недостаточно средств!");
    isProcessing = true; await changeBalance(-currentBet); isProcessing = false;
    minesActive = true; minesCoef = 1.0; minesSafeOpened = 0;
    document.querySelectorAll('.cell').forEach(c => { c.className = 'cell'; c.innerText = ''; c.style.pointerEvents = 'auto'; });
    document.getElementById('mines-btn').innerText = 'Забрать (0 🪙)';
    document.getElementById('mines-btn').style.background = '#34c759';
    document.getElementById('mines-current-coef').innerText = 'x1.00';
    document.getElementById('mines-cashout-amount').innerText = '0';
    playSound('click');
    window.mineIndices = [];
    while(window.mineIndices.length < selectedMines) {
        let r = Math.floor(Math.random() * 16);
        if(window.mineIndices.indexOf(r) === -1) window.mineIndices.push(r);
    }
}

function clickMineCell(cell) {
    if (!minesActive || isProcessing) return;
    const idx = parseInt(cell.dataset.index);
    if (cell.classList.contains('flipped')) return;
    isProcessing = true; cell.classList.add('flipped');
    playSound('click');
    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('soft');
    setTimeout(() => {
        if (window.mineIndices.includes(idx)) {
            cell.classList.add('bad'); cell.innerText = '💥';
            minesActive = false;
            document.querySelectorAll('.cell').forEach(c => c.style.pointerEvents = 'none');
            window.mineIndices.forEach(i => { const c = board.children[i]; if(!c.classList.contains('flipped')) { c.classList.add('flipped', 'bad'); c.innerText = '💣'; } });
            document.body.classList.add('shake'); setTimeout(() => document.body.classList.remove('shake'), 500);
            playSound('boom');
            const btn = document.getElementById('mines-btn');
            btn.innerText = `Играть снова`; btn.style.background = '#007aff';
        } else {
            cell.classList.add('good'); cell.innerText = '💎';
            minesSafeOpened++;
            let base = 1.0; if (selectedMines === 1) base = 1.05; if (selectedMines === 2) base = 1.15; if (selectedMines === 3) base = 1.30;
            let multiplier = 1 + (Math.log2(minesSafeOpened) * 0.3);
            minesCoef = base * multiplier;
            const cashout = Math.floor(currentBet * minesCoef);
            document.getElementById('mines-current-coef').innerText = `x${minesCoef.toFixed(2)}`;
            document.getElementById('mines-cashout-amount').innerText = cashout;
            document.getElementById('mines-btn').innerText = `Забрать (${cashout} 🪙)`;
        }
        isProcessing = false;
    }, 200);
}

loadUserData();