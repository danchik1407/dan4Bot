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

let crashInterval = null;
let crashBetActive = false;
let currentCrashMult = 1.0;
let crashState = { status: 'waiting' };

// SVG Ракеты (Красивая)
const SVG_ROCKET = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 50 50"><path fill="#fff" d="M25 0l-5 15h10zM20 15v20h10V15zM22 35v10h6V35z"/><path fill="#ff3b30" d="M22 35l-5 10 5-3zM28 35l5 10-5-3z"/></svg>`;
const imgRocket = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(SVG_ROCKET)}`;

// ================= ЗВУКИ (Тише) =================
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function playSound(type) {
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
        const bufferSize = audioCtx.sampleRate * 0.5;
        const buffer = audioCtx.createBuffer(1, bufferSize, audioCtx.sampleRate);
        const data = buffer.getChannelData(0);
        for (let i = 0; i < bufferSize; i++) data[i] = Math.random() * 2 - 1;
        const noise = audioCtx.createBufferSource(); noise.buffer = buffer;
        const noiseGain = audioCtx.createGain(); noiseGain.gain.setValueAtTime(0.1, now); noiseGain.gain.exponentialRampToValueAtTime(0.01, now + 0.5);
        noise.connect(noiseGain); noiseGain.connect(audioCtx.destination); noise.start(now);
    }
}

// ================= ХОККЕЙ =================
// (Код хоккея оставляем как был, он не менялся)

// ================= СУНДУКИ =================
// ...

// ================= ОБЩИЕ =================
// ...

// ================= МИНЫ =================
// ...

// ================= РАКЕТА (CRASH) =================
async function startCrashPolling() {
    if (crashInterval) clearInterval(crashInterval);
    pollCrashState();
    crashInterval = setInterval(pollCrashState, 100);
}

function stopCrashPolling() {
    if (crashInterval) clearInterval(crashInterval);
    crashInterval = null;
}

async function pollCrashState() {
    if (!document.getElementById('screen-crash').classList.contains('active')) return;
    try {
        const res = await fetch(`${API_URL}/api/crash_state`);
        const state = await res.json();
        crashState = state;

        const statusEl = document.getElementById('crash-status');
        const multEl = document.getElementById('crash-multiplier');
        const rocketEl = document.getElementById('crash-rocket');
        const btn = document.getElementById('crash-btn');
        const betContainer = document.getElementById('crash-bet-container');
        const historyEl = document.getElementById('crash-history-bar');

        if (state.status === 'waiting') {
            statusEl.innerText = `Ставки: ${state.timer} сек`;
            statusEl.style.color = '#ffcc00';
            multEl.innerText = 'x1.00';
            multEl.style.color = '#fff';
            betContainer.style.display = 'block';
            btn.innerText = 'Поставить ставку';
            btn.style.background = '#007aff';
            btn.disabled = false;
            crashBetActive = false;
            rocketEl.style.left = '50px';
            rocketEl.style.bottom = '50px';
            rocketEl.style.transform = 'rotate(0deg)';
            rocketEl.style.opacity = '1';
            document.getElementById('crash-flame').style.display = 'block';
            document.getElementById('crash-explosion').style.display = 'none';
            document.body.style.background = 'var(--bg)';

            // Обновление истории
            if (historyEl) {
                historyEl.innerHTML = '';
                (state.history || []).forEach(h => {
                    const div = document.createElement('div');
                    div.className = 'crash-hist-item';
                    div.innerText = `x${h.crash_point}`;
                    historyEl.appendChild(div);
                });
            }

        } else if (state.status === 'flying') {
            statusEl.innerText = 'Идёт полет...';
            statusEl.style.color = '#34c759';
            currentCrashMult = state.multiplier;
            multEl.innerText = `x${state.multiplier.toFixed(2)}`;
            betContainer.style.display = 'none';

            if (crashBetActive) {
                btn.innerText = `Забрать ${(parseInt(document.getElementById('crash-bet').value) * state.multiplier).toFixed(0)} 🪙`;
                btn.style.background = '#34c759';
            } else {
                btn.innerText = 'Вы не в игре';
                btn.style.background = '#555';
                btn.disabled = true;
            }

            // Движение ракеты
            const progress = Math.min(1, (state.multiplier - 1) / (state.crash_point - 1));
            const maxLeft = 250;
            const maxBottom = 250;
            const leftPos = 50 + progress * maxLeft;
            const bottomPos = 50 + progress * maxBottom;

            rocketEl.style.left = `${leftPos}px`;
            rocketEl.style.bottom = `${bottomPos}px`;
            rocketEl.style.transform = `rotate(${45 - progress * 90}deg)`;

        } else if (state.status === 'crashed') {
            statusEl.innerText = 'КРАШ!';
            statusEl.style.color = '#ff3b30';
            multEl.innerText = `x${state.crash_point.toFixed(2)}`;
            multEl.style.color = '#ff3b30';

            rocketEl.style.opacity = '0';
            document.getElementById('crash-flame').style.display = 'none';
            document.getElementById('crash-explosion').style.display = 'block';
            document.getElementById('crash-explosion').style.left = rocketEl.style.left;
            document.getElementById('crash-explosion').style.bottom = rocketEl.style.bottom;

            playSound('boom');
            if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('error');
            document.body.style.background = '#ff3b30';
            setTimeout(() => { document.body.style.background = 'var(--bg)'; }, 200);

            btn.innerText = 'Следующий раунд...';
            btn.style.background = '#555';
            btn.disabled = true;
            crashBetActive = false;

            // Обновление истории
            if (historyEl) {
                historyEl.innerHTML = '';
                (state.history || []).forEach(h => {
                    const div = document.createElement('div');
                    div.className = 'crash-hist-item';
                    div.innerText = `x${h.crash_point}`;
                    historyEl.appendChild(div);
                });
            }
        }

        const list = document.getElementById('crash-participants');
        list.innerHTML = '';
        state.players.forEach((p, i) => {
            const div = document.createElement('div');
            div.className = 'crash-part-item';
            const status = p.cashout_mult > 0 ? `<span style="color:#34c759">Забрал x${p.cashout_mult.toFixed(2)}</span>` : `<span style="color:#ff3b30">В игре</span>`;
            div.innerHTML = `<div>${p.name}</div><div>${p.bet} 🪙</div><div>${status}</div>`;
            list.appendChild(div);
        });

    } catch (e) { console.error(e); }
}

async function placeCrashBet() {
    if (isProcessing) return;
    const bet = parseInt(document.getElementById('crash-bet').value) || 10;
    if (balance < bet) return tg.showAlert("Недостаточно коинов!");

    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/crash_bet`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData, amount: bet })
        });
        if (!res.ok) { const err = await res.json(); isProcessing = false; return tg.showAlert(err.detail); }
        const data = await res.json();
        balance = data.new_balance; updateUI(); crashBetActive = true;
        playSound('click');
        document.getElementById('crash-btn').innerText = 'Ждем полет...';
        document.getElementById('crash-btn').disabled = true;
    } catch(e) { tg.showAlert("Ошибка"); }
    isProcessing = false;
}

async function cashoutCrash() {
    if (isProcessing) return;
    isProcessing = true;
    try {
        const res = await fetch(`${API_URL}/api/crash_cashout`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: tg.initData })
        });
        if (!res.ok) { isProcessing = false; return tg.showAlert("Не удалось забрать"); }
        const data = await res.json();

        balance = data.new_balance; updateUI();
        playSound('win');
        tg.showAlert(`Вы забрали ${data.win_amount} 🪙 на кэфе x${data.mult.toFixed(2)}!`);

        crashBetActive = false;
        document.getElementById('crash-btn').innerText = 'Забрано!';
        document.getElementById('crash-btn').disabled = true;
    } catch(e) { tg.showAlert("Ошибка"); }
    isProcessing = false;
}

function crashAction() {
    if (crashState.status === 'waiting') {
        placeCrashBet();
    } else if (crashState.status === 'flying' && crashBetActive) {
        cashoutCrash();
    }
}

loadUserData();