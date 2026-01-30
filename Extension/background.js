let socket = null;

function connect() {
    socket = new WebSocket('ws://localhost:8765');
    
    socket.onopen = () => {
        console.log('Connected to Sidebar App');
        sendTabData(); // Отправить данные СРАЗУ
    };

    socket.onmessage = (event) => {
        console.log('RAW message received:', event.data); // ЭТОТ ЛОГ ВАЖЕН
        try {
            const cmd = JSON.parse(event.data);
            const tabId = parseInt(cmd.id);

            switch (cmd.action) {
                case 'activate':
                    chrome.tabs.update(tabId, { active: true });
                    break;
                case 'close':
                    chrome.tabs.remove(tabId);
                    break;
                case 'duplicate':
                    chrome.tabs.duplicate(tabId);
                    break;
                case 'toggle_pin':
                    chrome.tabs.get(tabId, (tab) => {
                        chrome.tabs.update(tabId, { pinned: !tab.pinned });
                    });
                    break;
                case 'close_others':
                    chrome.tabs.query({ currentWindow: true }, (tabs) => {
                        const ids = tabs.filter(t => t.id !== tabId).map(t => t.id);
                        chrome.tabs.remove(ids);
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
                case 'remove_from_group':
                    chrome.tabs.ungroup(tabId);
                    break;
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
        setTimeout(connect, 2000);
    };

    socket.onerror = (err) => {
        console.error('Socket error:', err);
    };
}

// Периодический опрос для предотвращения "засыпания" Service Worker
setInterval(() => {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({type: "ping"})); // Просто чтобы сокет не дох
    }
}, 2000);

async function sendTabData() {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    try {
        const tabs = await chrome.tabs.query({ currentWindow: true });
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
    } catch (e) { console.error(e); }
}

// События
chrome.tabs.onUpdated.addListener(sendTabData);
chrome.tabs.onRemoved.addListener(sendTabData);
chrome.tabs.onActivated.addListener(sendTabData);
chrome.tabs.onMoved.addListener(sendTabData);
chrome.tabGroups.onUpdated.addListener(sendTabData);

connect();

//setInterval(sendTabData, 2000); // На всякий случай