let socket = null;

// ─── Keep-alive через Alarms API ────────────────────────────────────────────
// chrome.alarms надёжнее setInterval: Service Worker не засыпает между вызовами.
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 }); // каждые ~24 сек

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === 'keepAlive') {
        if (!socket || socket.readyState === WebSocket.CLOSED) {
            console.log('Alarm: socket closed, reconnecting...');
            connect();
        } else if (socket.readyState === WebSocket.OPEN) {
            try { socket.send(JSON.stringify({ type: "ping" })); } catch(e) {}
        }
    }
});

// ─── WebSocket ───────────────────────────────────────────────────────────────
function connect() {
    // Не создавать новое соединение, если уже есть активное или идёт подключение
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
        return;
    }

    socket = new WebSocket('ws://localhost:8765');

    socket.onopen = () => {
        console.log('Connected to Sidebar App');
        sendTabData();
    };

    socket.onmessage = (event) => {
        console.log('RAW message received:', event.data);
        try {
            const cmd = JSON.parse(event.data);

            if (cmd.type === 'ping') return;

            const tabId = parseInt(cmd.id);

            switch (cmd.action) {
                case 'activate':
                    chrome.tabs.update(tabId, { active: true });
                    break;

                case 'close':
                    chrome.tabs.remove(tabId);
                    break;

                // ── Мультизакрытие ──
                case 'close_multiple': {
                    const ids = (cmd.ids || []).map(id => parseInt(id)).filter(n => !isNaN(n));
                    if (ids.length > 0) chrome.tabs.remove(ids);
                    break;
                }

                case 'duplicate':
                    chrome.tabs.duplicate(tabId);
                    break;

                case 'toggle_pin':
                    chrome.tabs.get(tabId, (tab) => {
                        if (chrome.runtime.lastError) return;
                        chrome.tabs.update(tabId, { pinned: !tab.pinned });
                    });
                    break;

                case 'close_others':
                    chrome.tabs.query({ currentWindow: true }, (tabs) => {
                        const ids = tabs.filter(t => t.id !== tabId).map(t => t.id);
                        if (ids.length > 0) chrome.tabs.remove(ids);
                    });
                    break;

                case 'new_tab':
                    chrome.tabs.create({});
                    break;

                case 'add_to_group':
                    chrome.tabs.group({ tabIds: tabId, groupId: cmd.groupId });
                    break;

                case 'add_to_new_group':
                    chrome.tabs.group({ tabIds: tabId });
                    break;

                // ── Групповые операции для нескольких вкладок ──
                case 'add_multiple_to_group': {
                    const ids = (cmd.ids || []).map(id => parseInt(id)).filter(n => !isNaN(n));
                    if (ids.length > 0) chrome.tabs.group({ tabIds: ids, groupId: cmd.groupId });
                    break;
                }

                case 'add_multiple_to_new_group': {
                    const ids = (cmd.ids || []).map(id => parseInt(id)).filter(n => !isNaN(n));
                    if (ids.length > 0) chrome.tabs.group({ tabIds: ids });
                    break;
                }

                case 'remove_from_group':
                    chrome.tabs.ungroup(tabId);
                    break;

                case 'remove_multiple_from_group': {
                    const ids = (cmd.ids || []).map(id => parseInt(id)).filter(n => !isNaN(n));
                    if (ids.length > 0) chrome.tabs.ungroup(ids);
                    break;
                }

                case 'request_update':
                    sendTabData();
                    break;
            }
        } catch (err) {
            console.error('Error processing command:', err);
        }
    };

    socket.onclose = (e) => {
        console.log('Socket closed. Reconnecting in 2s...', e.reason);
        socket = null;
        setTimeout(connect, 2000);
    };

    socket.onerror = (err) => {
        console.error('Socket error:', err);
        // onclose сработает следом — не нужно дублировать reconnect здесь
    };
}

// ─── Отправка состояния вкладок ──────────────────────────────────────────────
async function sendTabData() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    try {
        const tabs = await chrome.tabs.query({ currentWindow: true, windowType: 'normal' });
        if (!tabs || tabs.length === 0) return;

        const groups = await chrome.tabGroups.query({});

        const data = {
            tabs: tabs.map(t => ({
                id: t.id, title: t.title, active: t.active,
                groupId: t.groupId, favIcon: t.favIconUrl
            })),
            groups: groups.map(g => ({
                id: g.id, title: g.title, color: g.color
            }))
        };
        socket.send(JSON.stringify(data));
    } catch (e) {
        console.error('sendTabData error:', e);
    }
}

// ─── Слушатели событий вкладок ────────────────────────────────────────────────
chrome.tabs.onUpdated.addListener(sendTabData);
chrome.tabs.onRemoved.addListener(sendTabData);
chrome.tabs.onActivated.addListener(sendTabData);
chrome.tabs.onMoved.addListener(sendTabData);
chrome.tabGroups.onUpdated.addListener(sendTabData);

connect();
