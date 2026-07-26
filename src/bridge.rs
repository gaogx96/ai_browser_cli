//! WebSocket bridge between agent-browser-cli and Chrome extension.
//!
//! This module implements a WebSocket server that the extension's Service Worker
//! connects to. It provides a request/response API for sending CDP commands to
//! Chrome via the extension's `chrome.debugger` API.
//!
//! # Protocol
//!
//! All messages are JSON over WebSocket.
//!
//! ## Request (Rust → Extension)
//! ```json
//! {"op": "attach", "cmdId": 1, "debuggee": {"tabId": 123}}
//! {"op": "detach", "cmdId": 2, "debuggee": {"tabId": 123}}
//! {"op": "getActiveTab", "cmdId": 3}
//! {"op": "sendCommand", "cmdId": 4, "debuggee": {"tabId": 123}, "method": "Runtime.evaluate", "params": {"expression": "document.title"}}
//! ```
//!
//! ## Response (Extension → Rust)
//! ```json
//! {"op": "result", "cmdId": 1, "ok": true, "data": {...}}
//! {"op": "result", "cmdId": 4, "ok": true, "data": {"result": {"value": "Page Title"}}}
//! {"op": "result", "cmdId": 2, "ok": false, "error": "Not attached"}
//! ```

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use futures::StreamExt;
use serde_json::Value;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{Mutex, oneshot};
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;

/// Timeout for waiting for a response from the extension.
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(10);
/// WebSocket server port.
const BRIDGE_PORT: u16 = 9223;

/// Shared state for the bridge.
struct BridgeInner {
    /// Pending requests waiting for extension response.
    pending: HashMap<u64, oneshot::Sender<Value>>,
    /// WebSocket sender to the extension (if connected).
    ws_tx: Option<tokio::sync::mpsc::UnboundedSender<Message>>,
}

/// Bridge handle for sending commands to the extension.
#[derive(Clone)]
pub struct Bridge {
    inner: Arc<Mutex<BridgeInner>>,
    next_cmd_id: Arc<AtomicU64>,
}

impl Bridge {
    fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(BridgeInner {
                pending: HashMap::new(),
                ws_tx: None,
            })),
            next_cmd_id: Arc::new(AtomicU64::new(1)),
        }
    }

    /// Send a command to the extension and wait for the response.
    ///
    /// Returns the `data` field from the extension's response on success.
    /// Returns an error if the timeout is reached or the extension returns `ok: false`.
    pub async fn send_to_extension(&self, mut op: Value) -> Result<Value> {
        let cmd_id = self.next_cmd_id.fetch_add(1, Ordering::SeqCst);
        op["cmdId"] = Value::from(cmd_id);

        let (tx, rx) = oneshot::channel::<Value>();

        {
            let mut inner = self.inner.lock().await;
            inner.pending.insert(cmd_id, tx);

            let ws_tx = inner
                .ws_tx
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("Extension not connected"))?;

            let msg_str = serde_json::to_string(&op)
                .with_context(|| format!("Failed to serialize command: {}", op))?;

            ws_tx
                .send(Message::Text(msg_str))
                .context("Failed to send message to extension (channel closed)")?;
        }

        // Wait for response with timeout
        match tokio::time::timeout(RESPONSE_TIMEOUT, rx).await {
            Ok(Ok(response)) => {
                let ok = response
                    .get("ok")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false);
                if ok {
                    Ok(response
                        .get("data")
                        .cloned()
                        .unwrap_or(Value::Null))
                } else {
                    let error = response
                        .get("error")
                        .and_then(|v| v.as_str())
                        .unwrap_or("Unknown error");
                    Err(anyhow::anyhow!("Extension error: {}", error))
                }
            }
            Ok(Err(_recv_err)) => {
                Err(anyhow::anyhow!(
                    "Extension response channel closed (cmd_id={})",
                    cmd_id
                ))
            }
            Err(_) => {
                // Timeout: clean up the pending entry
                let mut inner = self.inner.lock().await;
                inner.pending.remove(&cmd_id);
                Err(anyhow::anyhow!(
                    "Extension did not respond within {}s (cmd_id={})",
                    RESPONSE_TIMEOUT.as_secs(),
                    cmd_id
                ))
            }
        }
    }

    /// Wait for the extension to connect and return the active tab info.
    ///
    /// Polls `getActiveTab` every 500ms until the extension responds.
    pub async fn wait_for_extension(&self) -> Result<Value> {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
        let mut last_error = String::new();

        while tokio::time::Instant::now() < deadline {
            // Check if extension is connected
            {
                let inner = self.inner.lock().await;
                if inner.ws_tx.is_none() {
                    tokio::time::sleep(Duration::from_millis(500)).await;
                    continue;
                }
            }

            // Try to get the active tab
            let mut cmd = serde_json::json!({"op": "getActiveTab"});
            cmd["cmdId"] = Value::from(self.next_cmd_id.fetch_add(1, Ordering::SeqCst));

            match self.send_to_extension(cmd).await {
                Ok(tab_info) => {
                    eprintln!(
                        "[bridge] Extension connected, active tab: {} (id={})",
                        tab_info
                            .get("title")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown"),
                        tab_info
                            .get("tabId")
                            .and_then(|v| v.as_i64())
                            .unwrap_or(0),
                    );
                    return Ok(tab_info);
                }
                Err(e) => {
                    last_error = format!("{:#}", e);
                    // Extension might be connecting but not ready yet
                    tokio::time::sleep(Duration::from_millis(500)).await;
                }
            }
        }

        Err(anyhow::anyhow!(
            "Extension did not respond within 60s: {}",
            last_error
        ))
    }
}

/// Start the WebSocket server and wait for the extension to connect.
///
/// Returns a `Bridge` handle that can be used to send commands to the extension.
/// Events from the extension (CDP events, detached events) are forwarded to `event_tx`.
pub async fn start_bridge(event_tx: tokio::sync::mpsc::UnboundedSender<Value>) -> Result<Bridge> {
    let addr = format!("127.0.0.1:{}", BRIDGE_PORT);
    let listener = TcpListener::bind(&addr)
        .await
        .with_context(|| format!("Failed to bind bridge port {} — is it already in use?", BRIDGE_PORT))?;

    eprintln!("[bridge] Listening on ws://{}", addr);
    eprintln!("[bridge] Waiting for extension to connect...");

    let bridge = Bridge::new();
    let bridge_clone = bridge.clone();

    // Accept a single extension connection
    tokio::spawn(async move {
        match listener.accept().await {
            Ok((stream, peer_addr)) => {
                eprintln!("[bridge] Extension connecting from {}", peer_addr);
                if let Err(e) = handle_connection(stream, bridge_clone, event_tx).await {
                    eprintln!("[bridge] Connection error: {:#}", e);
                }
            }
            Err(e) => {
                eprintln!("[bridge] Accept error: {:#}", e);
            }
        }
    });

    Ok(bridge)
}

/// Handle a single WebSocket connection from the extension.
async fn handle_connection(
    stream: TcpStream,
    bridge: Bridge,
    event_tx: tokio::sync::mpsc::UnboundedSender<Value>,
) -> Result<()> {
    let ws_stream = accept_async(stream)
        .await
        .context("WebSocket handshake failed")?;

    eprintln!("[bridge] Extension WebSocket connected");

    let (ws_write, ws_read) = ws_stream.split();

    // Channel to send messages to the WebSocket writer task
    let (msg_tx, mut msg_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();

    // Register the writer channel so send_to_extension can use it
    {
        let mut inner = bridge.inner.lock().await;
        inner.ws_tx = Some(msg_tx.clone());
    }

    // Spawn writer task: forward messages from channel to WebSocket
    let write_handle = tokio::spawn(async move {
        use futures::SinkExt;
        let mut ws_write = ws_write;
        while let Some(msg) = msg_rx.recv().await {
            if let Err(e) = ws_write.send(msg).await {
                eprintln!("[bridge] WebSocket write error: {}", e);
                break;
            }
        }
    });

    // Reader task: handle incoming messages from extension
    let bridge_clone = bridge.clone();
    let read_handle = tokio::spawn(async move {
        let mut ws_read = ws_read;
        while let Some(Ok(msg)) = ws_read.next().await {
            match msg {
                Message::Text(text) => {
                    let value: Value = match serde_json::from_str(&text) {
                        Ok(v) => v,
                        Err(e) => {
                            eprintln!("[bridge] Invalid JSON from extension: {}", e);
                            continue;
                        }
                    };

                    let op = value.get("op").and_then(|v| v.as_str()).unwrap_or("");

                    match op {
                        "result" => {
                            // Route response to the pending request
                            let cmd_id = value.get("cmdId").and_then(|v| v.as_u64());
                            if let Some(cid) = cmd_id {
                                let mut inner = bridge_clone.inner.lock().await;
                                if let Some(tx) = inner.pending.remove(&cid) {
                                    let _ = tx.send(value);
                                } else {
                                    eprintln!(
                                        "[bridge] Orphaned result for cmd_id={}: no pending request",
                                        cid
                                    );
                                }
                            }
                        }
                        "ping" => {
                            // Heartbeat, ignore
                        }
                        "event" => {
                            // Forward CDP events to session router
                            let _ = event_tx.send(value);
                        }
                        "detached" => {
                            // Forward detach events to session router
                            let _ = event_tx.send(value);
                        }
                        _ => {
                            eprintln!("[bridge] Unknown message from extension: op={}", op);
                        }
                    }
                }
                Message::Close(_) => {
                    eprintln!("[bridge] Extension closed connection");
                    break;
                }
                Message::Ping(_) | Message::Pong(_) => {
                    // tungstenite handles ping/pong internally
                }
                Message::Binary(_) => {
                    eprintln!("[bridge] Unexpected binary message from extension");
                }
                _ => {}
            }
        }

        // Connection lost: clean up
        let mut inner = bridge_clone.inner.lock().await;
        inner.ws_tx = None;
        // Fail all pending requests
        for (_, tx) in inner.pending.drain() {
            let _ = tx.send(serde_json::json!({"ok": false, "error": "Extension disconnected"}));
        }
        eprintln!("[bridge] Extension disconnected");
    });

    // Wait for either task to finish
    tokio::select! {
        _ = write_handle => {},
        _ = read_handle => {},
    }

    Ok(())
}