//! Session Router — translates between CDP's sessionId model and
//! chrome.debugger's Debuggee (tabId) model.
//!
//! This is the core of the CDP façade. It sits between chromiumoxide and
//! the extension bridge, translating CDP commands and events.
//!
//! # Flow
//!
//! chromiumoxide →┌─────────────────────────────────────────┐→ extension
//!   {id,method,  │  SessionRouter                           │   {op:"sendCommand",debuggee:{tabId},method,params}
//!    params,     │                                         │
//!    sessionId?} │  Target.* commands → handled locally     │
//!                │  Browser.* commands → handled locally    │
//!                │  Other commands → forwarded to extension │
//!                │                                         │
//! extension →└─────────────────────────────────────────┘→ chromiumoxide
//!   {op:"event",  │  Look up sessionId from tabId          │   {method,params,sessionId}
//!    source:      │                                         │
//!    {tabId},     │                                         │
//!    method,      │                                         │
//!    params}      │                                         │
//!                └─────────────────────────────────────────┘

use std::collections::HashMap;
use std::sync::atomic::AtomicU64;

use anyhow::Result;
use serde_json::Value;
use tokio::sync::Mutex;

use crate::bridge::Bridge;

/// Information about a session.
struct SessionInfo {
    tab_id: i64,
    target_id: String,
}

/// Pending target creation (before attachToTarget completes).
struct PendingTarget {
    tab_id: i64,
    #[allow(dead_code)]
    url: String,
}

/// SessionRouter handles the translation between CDP and chrome.debugger.
pub struct SessionRouter {
    /// Bridge to send commands to the extension.
    bridge: Bridge,
    /// Sender to write CDP messages back to the facade (→ chromiumoxide).
    /// Wrapped in Option so it can be set after facade starts.
    facade_tx: tokio::sync::Mutex<Option<tokio::sync::mpsc::UnboundedSender<Value>>>,
    /// sessionId → SessionInfo mapping.
    sessions: Mutex<HashMap<String, SessionInfo>>,
    /// tabId → sessionId mapping (reverse lookup).
    tab_to_session: Mutex<HashMap<i64, String>>,
    /// Pending targets waiting for attachToTarget (targetId → tabId).
    pending_targets: Mutex<HashMap<String, PendingTarget>>,
    /// Whether auto-attach mode is enabled.
    auto_attach: Mutex<bool>,
    /// Session ID counter.
    next_session_id: AtomicU64,
    /// Command ID counter for local responses (reserved for future use).
    #[allow(dead_code)]
    next_cmd_id: AtomicU64,
}

impl SessionRouter {
    /// Create a new SessionRouter.
    pub fn new(
        bridge: Bridge,
        facade_tx: tokio::sync::mpsc::UnboundedSender<Value>,
    ) -> Self {
        Self {
            bridge,
            facade_tx: tokio::sync::Mutex::new(Some(facade_tx)),
            sessions: Mutex::new(HashMap::new()),
            tab_to_session: Mutex::new(HashMap::new()),
            pending_targets: Mutex::new(HashMap::new()),
            auto_attach: Mutex::new(false),
            next_session_id: AtomicU64::new(1),
            next_cmd_id: AtomicU64::new(1),
        }
    }

    /// Set the facade_tx after construction (used when facade starts after router).
    pub async fn set_facade_tx(&self, tx: tokio::sync::mpsc::UnboundedSender<Value>) {
        let mut lock = self.facade_tx.lock().await;
        *lock = Some(tx);
    }

    /// Helper to send a message to the facade (→ chromiumoxide).
    async fn send_to_facade(&self, msg: serde_json::Value) {
        let lock = self.facade_tx.lock().await;
        if let Some(tx) = lock.as_ref() {
            let _ = tx.send(msg);
        }
    }

    /// Generate a new unique session ID.
    fn new_session_id(&self) -> String {
        use std::sync::atomic::Ordering;
        format!("session-{}", self.next_session_id.fetch_add(1, Ordering::SeqCst))
    }

    /// Generate a new unique command ID for local responses.
    #[allow(dead_code)]
    fn new_cmd_id(&self) -> u64 {
        use std::sync::atomic::Ordering;
        self.next_cmd_id.fetch_add(1, Ordering::SeqCst)
    }

    /// Handle a CDP message from chromiumoxide (via the facade).
    ///
    /// `msg` is the raw parsed JSON of a CDP message:
    /// ```json
    /// {"id": 1, "method": "Target.setDiscoverTargets", "params": {...}}
    /// {"id": 2, "method": "Page.navigate", "params": {...}, "sessionId": "session-1"}
    /// ```
    pub async fn handle_cdp_message(&self, msg: Value) {
        let method = msg.get("method").and_then(|v| v.as_str()).unwrap_or("");
        let cmd_id = msg.get("id").and_then(|v| v.as_u64());
        let session_id = msg.get("sessionId").and_then(|v| v.as_str()).map(|s| s.to_string());
        let params = msg.get("params").unwrap_or(&Value::Null).clone();

        // Log all incoming CDP messages for debugging
        eprintln!(
            "[cdp] → {} (id={:?}, sessionId={:?})",
            method, cmd_id, session_id
        );

        // If the message has a sessionId, it's a page-level command → forward to extension
        if let Some(ref sid) = session_id {
            // Intercept commands that chrome.debugger doesn't allow.
            match method {
                "Target.activateTarget" => {
                    if let Some(id) = cmd_id {
                        let response = serde_json::json!({
                            "id": id,
                            "result": {},
                            "sessionId": sid,
                        });
                        self.send_to_facade(response).await;
                    }
                    return;
                }
                // Page.captureScreenshot: no special handling needed for chrome.debugger.
                // The tab created by createTarget is the active tab, so screenshots work.
                "Page.captureScreenshot" => {}
                _ => {}
            }

            let sessions = self.sessions.lock().await;
            if let Some(session) = sessions.get(sid) {
                let debuggee = serde_json::json!({"tabId": session.tab_id});
                let send_cmd = serde_json::json!({
                    "op": "sendCommand",
                    "debuggee": debuggee,
                    "method": method,
                    "params": params,
                });

                match self.bridge.send_to_extension(send_cmd).await {
                    Ok(result) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "result": result,
                            "sessionId": sid,
                        });
                        self.send_to_facade(response).await;
                    }
                    Err(e) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "error": {
                                "code": -32000,
                                "message": format!("{:#}", e)
                            },
                            "sessionId": sid,
                        });
                        self.send_to_facade(response).await;
                    }
                }
            } else {
                // Unknown sessionId — this shouldn't happen
                let response = serde_json::json!({
                    "id": cmd_id,
                    "error": {
                        "code": -32000,
                        "message": format!("Unknown sessionId: {}", sid)
                    },
                });
                self.send_to_facade(response).await;
            }
            return;
        }

        // Browser-level command (no sessionId)
        match method {
            "Target.setDiscoverTargets" => {
                let response = serde_json::json!({
                    "id": cmd_id,
                    "result": {},
                });
                self.send_to_facade(response).await;
            }

            "Target.setAutoAttach" => {
                let auto = params.get("autoAttach").and_then(|v| v.as_bool()).unwrap_or(false);
                *self.auto_attach.lock().await = auto;
                eprintln!("[cdp] Target.setAutoAttach: autoAttach={}", auto);
                let response = serde_json::json!({
                    "id": cmd_id,
                    "result": {},
                });
                self.send_to_facade(response).await;
            }

            "Target.getTargets" => {
                match self.get_targets().await {
                    Ok(targets) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "result": { "targetInfos": targets },
                        });
                        self.send_to_facade(response).await;
                    }
                    Err(e) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "error": { "code": -32000, "message": format!("{:#}", e) },
                        });
                        self.send_to_facade(response).await;
                    }
                }
            }

            "Target.createTarget" => {
                let url = params.get("url").and_then(|v| v.as_str()).unwrap_or("about:blank");
                match self.create_target(url).await {
                    Ok(result) => {
                        let target_id = result.get("targetId").and_then(|v| v.as_str()).unwrap_or("");
                        let tab_id = result.get("tabId").and_then(|v| v.as_i64()).unwrap_or(0);

                        // Store pending target (don't attach yet — attachToTarget will do it)
                        {
                            let mut pending = self.pending_targets.lock().await;
                            pending.insert(target_id.to_string(), PendingTarget {
                                tab_id,
                                url: url.to_string(),
                            });
                        }

                        // MUST send Target.targetCreated BEFORE the response,
                        // because chromiumoxide's handler looks up the target
                        // in its targets map immediately after receiving the
                        // createTarget response.
                        let target_created = serde_json::json!({
                            "method": "Target.targetCreated",
                            "params": {
                                "targetInfo": {
                                    "targetId": target_id,
                                    "type": "page",
                                    "title": "",
                                    "url": url,
                                    "attached": false,
                                    "canAccessOpener": false,
                                    "browserContextId": "default",
                                },
                            },
                        });
                        self.send_to_facade(target_created).await;

                        // Respond with targetId
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "result": { "targetId": target_id },
                        });
                        self.send_to_facade(response).await;
                    }
                    Err(e) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "error": { "code": -32000, "message": format!("{:#}", e) },
                        });
                        self.send_to_facade(response).await;
                    }
                }
            }

            "Target.attachToTarget" => {
                let target_id = params.get("targetId").and_then(|v| v.as_str()).unwrap_or("");

                match self.attach_to_target(target_id).await {
                    Ok(session_id) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "result": { "sessionId": session_id },
                        });
                        self.send_to_facade(response).await;

                        // Push attachedToTarget event
                        let target_info = serde_json::json!({
                            "targetId": target_id,
                            "type": "page",
                            "title": "",
                            "url": "about:blank",
                            "attached": true,
                            "canAccessOpener": false,
                            "browserContextId": "default",
                        });
                        let event = serde_json::json!({
                            "method": "Target.attachedToTarget",
                            "params": {
                                "sessionId": session_id,
                                "targetInfo": target_info,
                                "waitingForDebugger": false,
                            },
                        });
                        self.send_to_facade(event).await;
                    }
                    Err(e) => {
                        let response = serde_json::json!({
                            "id": cmd_id,
                            "error": { "code": -32000, "message": format!("{:#}", e) },
                        });
                        self.send_to_facade(response).await;
                    }
                }
            }

            "Target.closeTarget" => {
                let target_id = params.get("targetId").and_then(|v| v.as_str()).unwrap_or("");
                // Find the tab_id and session_id for this target
                let sessions = self.sessions.lock().await;
                let session_to_remove: Vec<String> = sessions.iter()
                    .filter(|(_, info)| info.target_id == target_id)
                    .map(|(sid, _)| sid.clone())
                    .collect();

                for sid in &session_to_remove {
                    if let Some(info) = sessions.get(sid) {
                        let _ = self.bridge.send_to_extension(serde_json::json!({
                            "op": "detach",
                            "debuggee": {"tabId": info.tab_id},
                        })).await;
                    }
                }
                drop(sessions);

                // Clean up session mappings
                let mut sessions = self.sessions.lock().await;
                let mut tab_map = self.tab_to_session.lock().await;
                for sid in &session_to_remove {
                    if let Some(info) = sessions.remove(sid) {
                        tab_map.remove(&info.tab_id);
                    }
                }

                let response = serde_json::json!({
                    "id": cmd_id,
                    "result": { "success": true },
                });
                self.send_to_facade(response).await;
            }

            "Target.getTargetInfo" => {
                // Return a basic target info
                let target_id = params.get("targetId").and_then(|v| v.as_str()).unwrap_or("");
                let target_info = serde_json::json!({
                    "targetId": target_id,
                    "type": "page",
                    "title": "",
                    "url": "about:blank",
                    "attached": true,
                    "canAccessOpener": false,
                    "browserContextId": "default",
                });
                let response = serde_json::json!({
                    "id": cmd_id,
                    "result": { "targetInfo": target_info },
                });
                self.send_to_facade(response).await;
            }

            "Browser.getVersion" => {
                let response = serde_json::json!({
                    "id": cmd_id,
                    "result": {
                        "protocolVersion": "1.3",
                        "product": "Chrome/120.0.0.0",
                        "revision": "",
                        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "jsVersion": "12.0.0.0",
                    },
                });
                self.send_to_facade(response).await;
            }

            // Unknown browser-level commands — log and return empty success
            _ => {
                eprintln!("[cdp] ⚠️ Unhandled browser-level command: {}", method);
                if let Some(id) = cmd_id {
                    let response = serde_json::json!({
                        "id": id,
                        "result": {},
                    });
                    self.send_to_facade(response).await;
                }
            }
        }
    }

    /// Handle an event from the extension (CDP events forwarded by the bridge).
    ///
    /// `msg` is the raw event message from the extension:
    /// ```json
    /// {"op":"event", "source":{"tabId":123}, "method":"Page.frameStartedLoading", "params":{...}}
    /// ```
    pub async fn handle_extension_event(&self, msg: Value) {
        let op = msg.get("op").and_then(|v| v.as_str()).unwrap_or("");

        match op {
            "event" => {
                let tab_id = msg.get("source").and_then(|v| v.get("tabId")).and_then(|v| v.as_i64());
                let method = msg.get("method").and_then(|v| v.as_str()).unwrap_or("");
                let params = msg.get("params").cloned().unwrap_or(Value::Null);

                if let Some(tid) = tab_id {
                    let tab_map = self.tab_to_session.lock().await;
                    if let Some(session_id) = tab_map.get(&tid) {
                        let event = serde_json::json!({
                            "method": method,
                            "params": params,
                            "sessionId": session_id,
                        });
                        self.send_to_facade(event).await;
                    } else {
                        // Unknown tab — forward without sessionId as a browser-level event
                        let event = serde_json::json!({
                            "method": method,
                            "params": params,
                        });
                        self.send_to_facade(event).await;
                    }
                }
            }
            "detached" => {
                let reason = msg.get("reason").and_then(|v| v.as_str()).unwrap_or("unknown");
                let tab_id = msg.get("source").and_then(|v| v.get("tabId")).and_then(|v| v.as_i64());
                eprintln!("[cdp] Debugger detached from tab {:?}: {}", tab_id, reason);

                // Clean up the session
                if let Some(tid) = tab_id {
                    let mut tab_map = self.tab_to_session.lock().await;
                    if let Some(session_id) = tab_map.remove(&tid) {
                        let mut sessions = self.sessions.lock().await;
                        sessions.remove(&session_id);
                    }
                }
            }
            _ => {}
        }
    }

    // ─── Private helpers ──────────────────────────────────────────────────

    /// Get current targets from the extension via chrome.debugger.getTargets().
    async fn get_targets(&self) -> Result<Value> {
        let cmd = serde_json::json!({"op": "getTargets"});
        let result = self.bridge.send_to_extension(cmd).await?;
        Ok(result.get("targets").cloned().unwrap_or(Value::Array(vec![])))
    }

    /// Create a new tab and attach debugger to it.
    async fn create_target(&self, url: &str) -> Result<Value> {
        let cmd = serde_json::json!({"op": "createTab", "url": url});
        let result = self.bridge.send_to_extension(cmd).await?;
        let tab_id = result.get("tabId").and_then(|v| v.as_i64())
            .ok_or_else(|| anyhow::anyhow!("createTab did not return a tabId"))?;

        // Don't attach debugger yet — chromiumoxide will send Target.attachToTarget
        // which will handle the attachment and session creation.

        // Get the target ID from chrome.debugger.getTargets()
        let target_id = self.find_target_id_for_tab(tab_id).await
            .unwrap_or_else(|| format!("tab-{}", tab_id));

        eprintln!("[cdp] Created tab {} with targetId={}", tab_id, target_id);

        Ok(serde_json::json!({
            "tabId": tab_id,
            "targetId": target_id,
        }))
    }

    /// Attach debugger to an existing target (by targetId).
    async fn attach_to_target(&self, target_id: &str) -> Result<String> {
        // First check pending_targets (created by createTarget)
        let tab_id = {
            let pending = self.pending_targets.lock().await;
            pending.get(target_id).map(|p| p.tab_id)
        };

        // Fall back to getTargets if not found in pending
        let tab_id = match tab_id {
            Some(id) => id,
            None => {
                let targets = self.get_targets().await?;
                targets.as_array().and_then(|arr| {
                    arr.iter().find(|t| {
                        t.get("id").and_then(|v| v.as_str()) == Some(target_id)
                            || t.get("targetId").and_then(|v| v.as_str()) == Some(target_id)
                    })
                }).and_then(|t| {
                    t.get("tabId").or_else(|| t.get("id")).and_then(|v| v.as_i64())
                }).ok_or_else(|| anyhow::anyhow!("Target {} not found", target_id))?
            }
        };

        // Clean up pending
        {
            let mut pending = self.pending_targets.lock().await;
            pending.remove(target_id);
        }

        // Attach debugger to the tab
        let attach_cmd = serde_json::json!({
            "op": "attach",
            "debuggee": {"tabId": tab_id},
        });
        self.bridge.send_to_extension(attach_cmd).await?;

        // Generate sessionId
        let session_id = self.new_session_id();
        {
            let mut sessions = self.sessions.lock().await;
            let mut tab_map = self.tab_to_session.lock().await;
            sessions.insert(session_id.clone(), SessionInfo {
                tab_id,
                target_id: target_id.to_string(),
            });
            tab_map.insert(tab_id, session_id.clone());
        }

        Ok(session_id)
    }

    /// Push a Target.attachedToTarget event to chromiumoxide.
    #[allow(dead_code)]
    async fn push_attached_event(&self, session_id: &str, target_id: &str, url: &str) {
        let target_info = serde_json::json!({
            "targetId": target_id,
            "type": "page",
            "title": "",
            "url": url,
            "attached": true,
            "canAccessOpener": false,
            "browserContextId": "default",
        });
        let event = serde_json::json!({
            "method": "Target.attachedToTarget",
            "params": {
                "sessionId": session_id,
                "targetInfo": target_info,
                "waitingForDebugger": false,
            },
        });
        self.send_to_facade(event).await;
    }

    /// Find the chrome.debugger target ID for a given tab ID.
    async fn find_target_id_for_tab(&self, tab_id: i64) -> Option<String> {
        match self.get_targets().await {
            Ok(targets) => {
                targets.as_array()?.iter().find_map(|t| {
                    let tid = t.get("tabId").and_then(|v| v.as_i64())?;
                    if tid == tab_id {
                        t.get("id").or_else(|| t.get("targetId")).and_then(|v| v.as_str().map(|s| s.to_string()))
                    } else {
                        None
                    }
                })
            }
            Err(_) => None,
        }
    }
}