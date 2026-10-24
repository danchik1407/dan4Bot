const tg = window.Telegram.WebApp;
tg.expand();

const user = tg.initDataUnsafe?.user;
if (user) {
    document.getElementById('user-name').innerText = user.first_name;
    document.getElementById('profile-id').innerText = user.id;
    if (user.photo_url) document.getElementById('user-avatar').src = user.photo_url;
}

// Заглушка баланса (потом заменим на реальный)
document.getElementById('user-balance').innerText = "0.00";

function switchTab(tabId, element) {
    document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
    document.getElementById('tab-' + tabId).classList.add('active');

    document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
    element.classList.add('active');

    if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred('light');
}

// Заглушка топа
document.getElementById('leaderboard-list').innerHTML = `
    <div><span>🥇 Dan4ik</span> <span>5 000</span></div>
    <div><span>🥈 Вася</span> <span>3 200</span></div>
    <div><span>🥉 Петя</span> <span>1 500</span></div>
`;