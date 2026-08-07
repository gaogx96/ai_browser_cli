// Agent Browser Bridge — Service Worker
// 职责：WebSocket 客户端连接本地 Rust bridge，通过 chrome.debugger API 执行 CDP 命令
//
// 内部协议（JSON over WebSocket）：
//  Rust → SW: {op, cmdId, ...}  op ∈ attach|detach|getActiveTab|getTargets|createTab|sendCommand
//  SW → Rust: {op:"result", cmdId, ok, data?, error?}
//  SW → Rust: {op:"event", source, method, params}  (chrome.debugger 事件)
//  SW → Rust: {op:"detached", source, reason}        (debugger 断开)
//  SW → Rust: {op:"ping"}                             (心跳)

const BRIDGE_URL = "ws://127.0.0.1:9223";
let ws = null;
let keepAliveTimer = null;

// ─── WebSocket 连接管理 ───────────────────────────────────────────────

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  try {
    ws = new WebSocket(BRIDGE_URL);
  } catch (e) {
    console.error("[agent-bridge] WebSocket creation failed:", e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log("[agent-bridge] Connected to bridge at", BRIDGE_URL);
    startHeartbeat();
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    } catch (e) {
      console.error("[agent-bridge] Failed to parse message:", e);
    }
  };

  ws.onclose = (event) => {
    console.log("[agent-bridge] Disconnected (code:", event.code, "reason:", event.reason, ")");
    stopHeartbeat();
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = (event) => {
    console.error("[agent-bridge] WebSocket error");
  };
}

function scheduleReconnect() {
  setTimeout(() => {
    console.log("[agent-bridge] Attempting reconnect...");
    connect();
  }, 1000);
}

function startHeartbeat() {
  stopHeartbeat();
  keepAliveTimer = setInterval(() => {
    try {
      ws?.send(JSON.stringify({ op: "ping" }));
    } catch (_) {}
  }, 20000);
}

function stopHeartbeat() {
  if (keepAliveTimer) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
}

// ─── 消息处理 ─────────────────────────────────────────────────────────

async function handleMessage(msg) {
  const { op, cmdId } = msg;

  try {
    switch (op) {
      case "attach":
        await handleAttach(msg, cmdId);
        break;
      case "detach":
        await handleDetach(msg, cmdId);
        break;
      case "getActiveTab":
        await handleGetActiveTab(cmdId);
        break;
      case "getTargets":
        await handleGetTargets(cmdId);
        break;
      case "createTab":
        await handleCreateTab(msg, cmdId);
        break;
      case "sendCommand":
        await handleSendCommand(msg, cmdId);
        break;
      case "activateTab":
        await handleActivateTab(msg, cmdId);
        break;
      case "download":
        await handleDownload(msg, cmdId);
        break;
      default:
        console.warn("[agent-bridge] Unknown op:", op);
        reply(cmdId, { ok: false, error: `Unknown op: ${op}` });
    }
  } catch (err) {
    console.error(`[agent-bridge] Error handling ${op}:`, err);
    reply(cmdId, { ok: false, error: String(err.message ?? err) });
  }
}

async function handleAttach(msg, cmdId) {
  const debuggee = msg.debuggee;
  if (!debuggee) {
    reply(cmdId, { ok: false, error: "Missing 'debuggee' field" });
    return;
  }
  await chrome.debugger.attach(debuggee, "1.3");
  reply(cmdId, { ok: true, data: { attached: true } });
}

async function handleDetach(msg, cmdId) {
  const debuggee = msg.debuggee;
  if (!debuggee) {
    reply(cmdId, { ok: false, error: "Missing 'debuggee' field" });
    return;
  }
  await chrome.debugger.detach(debuggee);
  reply(cmdId, { ok: true, data: { detached: true } });
}

async function handleGetActiveTab(cmdId) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tabs || tabs.length === 0) {
    reply(cmdId, { ok: false, error: "No active tab found" });
    return;
  }
  const tab = tabs[0];
  reply(cmdId, {
    ok: true,
    data: {
      tabId: tab.id,
      url: tab.url,
      title: tab.title,
    },
  });
}

async function handleGetTargets(cmdId) {
  const targets = await chrome.debugger.getTargets();
  reply(cmdId, { ok: true, data: { targets } });
}

async function handleCreateTab(msg, cmdId) {
  const url = msg.url ?? "about:blank";
  // active: false — 不激活新标签页，避免抢用户焦点
  const tab = await chrome.tabs.create({ url, active: false });
  reply(cmdId, { ok: true, data: { tabId: tab.id } });
}

async function handleActivateTab(msg, cmdId) {
  const tabId = msg.tabId;
  if (!tabId) {
    reply(cmdId, { ok: false, error: "Missing 'tabId' field for activateTab" });
    return;
  }
  await chrome.tabs.update(tabId, { active: true });
  reply(cmdId, { ok: true, data: { activated: true } });
}

async function handleDownload(msg, cmdId) {
  const url = msg.url;
  const filename = msg.filename ?? "";

  if (!url) {
    reply(cmdId, { ok: false, error: "Missing 'url' field for download" });
    return;
  }

  try {
    const downloadId = await chrome.downloads.download({ url, filename: filename || undefined });
    console.log("[agent-bridge] Download started, id:", downloadId);

    // Wait for completion
    const result = await new Promise((resolve) => {
      const handler = async (delta) => {
        if (delta.id === downloadId && delta.state) {
          const state = delta.state.current;
          if (state === "complete") {
            chrome.downloads.onChanged.removeListener(handler);
            // Get the filename via search API
            try {
              const items = await chrome.downloads.search({ id: downloadId });
              const finalPath = (items && items[0]) ? (items[0].filename || "") : "";
              resolve({ ok: true, data: { downloadId, state: "complete", filename: finalPath } });
            } catch (_) {
              resolve({ ok: true, data: { downloadId, state: "complete", filename: "(unknown)" } });
            }
          } else if (state === "interrupted") {
            chrome.downloads.onChanged.removeListener(handler);
            resolve({ ok: false, error: `Download interrupted: ${delta.error?.current ?? "unknown"}` });
          }
        }
      };
      chrome.downloads.onChanged.addListener(handler);
      // Timeout after 60s
      setTimeout(() => {
        chrome.downloads.onChanged.removeListener(handler);
        resolve({ ok: false, error: "Download timed out" });
      }, 60000);
    });

    reply(cmdId, result);
  } catch (err) {
    reply(cmdId, { ok: false, error: String(err.message ?? err) });
  }
}

async function handleSendCommand(msg, cmdId) {
  const debuggee = msg.debuggee;
  const method = msg.method;
  const params = msg.params ?? {};

  if (!debuggee) {
    reply(cmdId, { ok: false, error: "Missing 'debuggee' field" });
    return;
  }
  if (!method) {
    reply(cmdId, { ok: false, error: "Missing 'method' field" });
    return;
  }

  const result = await chrome.debugger.sendCommand(debuggee, method, params);
  reply(cmdId, { ok: true, data: result });
}

function reply(cmdId, payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn("[agent-bridge] Cannot reply, WebSocket not open");
    return;
  }
  const response = { op: "result", cmdId, ...payload };
  ws.send(JSON.stringify(response));
}

// ─── chrome.debugger 事件转发 ────────────────────────────────────────

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ op: "event", source, method, params }));
  }
});

chrome.debugger.onDetach.addListener((source, reason) => {
  console.log("[agent-bridge] Debugger detached:", source, reason);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ op: "detached", source, reason }));
  }
});

// ─── SW 生命周期 ──────────────────────────────────────────────────────

connect();

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(() => {
  console.log("[agent-bridge] Extension installed/updated");
  connect();
});

chrome.alarms.create("bridge-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bridge-reconnect") {
    if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
      console.log("[agent-bridge] Alarm triggered reconnect");
      connect();
    }
  }
});