//! CDP Façade — WebSocket server that speaks raw CDP to chromiumoxide.
//!
//! Listens on port 9224, accepts a single connection from chromiumoxide's
//! `Browser::connect("ws://127.0.0.1:9224/devtools/browser/agent")`.
//! All CDP messages are forwarded to the SessionRouter for processing.

use std::sync::Arc;

use anyhow::{Context, Result};
use futures::StreamExt;
use serde_json::Value;
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::accept_async;
use tokio_tungstenite::tungstenite::Message;

use crate::session_router::SessionRouter;

/// Port for the CDP façade WebSocket server.
const FACADE_PORT: u16 = 9224;

/// Facade handle for sending CDP messages back to chromiumoxide.
pub struct Facade {
    /// Channel to send raw CDP messages to the facade's WebSocket writer.
    /// These are forwarded to chromiumoxide.
    pub facade_tx: tokio::sync::mpsc::UnboundedSender<Value>,
}

/// Start the CDP façade WebSocket server on port 9224.
///
/// Accepts a single connection from chromiumoxide and wires it to the
/// SessionRouter. Returns a `Facade` handle that can be used to send
/// CDP responses back to chromiumoxide.
pub async fn start_facade(router: Arc<SessionRouter>) -> Result<Facade> {
    let addr = format!("127.0.0.1:{}", FACADE_PORT);
    let listener = TcpListener::bind(&addr)
        .await
        .with_context(|| format!("Failed to bind facade port {} — is it already in use?", FACADE_PORT))?;

    eprintln!("[facade] Listening on ws://{}", addr);
    eprintln!("[facade] Waiting for chromiumoxide to connect...");

    // Channel to send CDP messages to the facade's WebSocket writer
    let (facade_tx, facade_rx) = tokio::sync::mpsc::unbounded_channel::<Value>();

    let facade = Facade {
        facade_tx: facade_tx.clone(),
    };

    tokio::spawn(async move {
        match listener.accept().await {
            Ok((stream, peer_addr)) => {
                eprintln!("[facade] Chromiumoxide connecting from {}", peer_addr);
                if let Err(e) = handle_facade_connection(stream, router, facade_tx, facade_rx).await {
                    eprintln!("[facade] Connection error: {:#}", e);
                }
            }
            Err(e) => {
                eprintln!("[facade] Accept error: {:#}", e);
            }
        }
    });

    Ok(facade)
}

/// Handle a single WebSocket connection from chromiumoxide.
async fn handle_facade_connection(
    stream: TcpStream,
    router: Arc<SessionRouter>,
    _cdp_tx: tokio::sync::mpsc::UnboundedSender<Value>,
    mut cdp_rx: tokio::sync::mpsc::UnboundedReceiver<Value>,
) -> Result<()> {
    let ws_stream = accept_async(stream)
        .await
        .context("Facade WebSocket handshake failed")?;

    eprintln!("[facade] Chromiumoxide WebSocket connected");

    let (ws_write, ws_read) = ws_stream.split();

    // Shared channel to send to writer
    let (_msg_tx, mut msg_rx) = tokio::sync::mpsc::unbounded_channel::<Message>();

    // Spawn writer task: reads from both cdp_rx (from SessionRouter) and msg_rx
    let write_handle = tokio::spawn(async move {
        use futures::SinkExt;
        let mut ws_write = ws_write;

        loop {
            tokio::select! {
                // CDP responses from SessionRouter
                Some(cdp_msg) = cdp_rx.recv() => {
                    let json_str = match serde_json::to_string(&cdp_msg) {
                        Ok(s) => s,
                        Err(e) => {
                            eprintln!("[facade] Failed to serialize CDP response: {}", e);
                            continue;
                        }
                    };
                    eprintln!("[cdp] ← {}", &json_str[..json_str.char_indices().map(|(i,_)|i).nth(200).unwrap_or(json_str.len())]);
                    if let Err(e) = ws_write.send(Message::Text(json_str)).await {
                        eprintln!("[facade] WebSocket write error: {}", e);
                        break;
                    }
                }
                // Other messages (not currently used, but reserved)
                Some(msg) = msg_rx.recv() => {
                    if let Err(e) = ws_write.send(msg).await {
                        eprintln!("[facade] WebSocket write error: {}", e);
                        break;
                    }
                }
                else => break,
            }
        }
    });

    // Reader task: forwards CDP messages from chromiumoxide to SessionRouter
    let read_handle = tokio::spawn(async move {
        let mut ws_read = ws_read;
        while let Some(Ok(msg)) = ws_read.next().await {
            let text = match msg {
                Message::Text(t) => t,
                Message::Close(_) => {
                    eprintln!("[facade] Chromiumoxide closed connection");
                    break;
                }
                Message::Ping(_) | Message::Pong(_) => continue,
                Message::Binary(_) => {
                    eprintln!("[facade] Unexpected binary message");
                    continue;
                }
                _ => continue,
            };

            let value: Value = match serde_json::from_str(&text) {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("[facade] Invalid JSON from chromiumoxide: {}", e);
                    continue;
                }
            };

            let trunc = &text[..text.char_indices().map(|(i,_)|i).nth(200).unwrap_or(text.len())];
            eprintln!("[cdp] → {} (method={:?})",
                trunc,
                value.get("method").and_then(|v| v.as_str()).unwrap_or("(no method)")
            );

            router.handle_cdp_message(value).await;
        }

        eprintln!("[facade] Chromiumoxide disconnected");
    });

    // Wait for either task to finish
    tokio::select! {
        _ = write_handle => {},
        _ = read_handle => {},
    }

    Ok(())
}