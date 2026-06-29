/// MCP (Model Context Protocol) server for agent-browser-cli.
///
/// Implements JSON-RPC 2.0 over stdin/stdout newline-delimited JSON,
/// exposing browser automation actions as MCP tools.
///
/// Protocol flow:
///   1. Client sends `initialize` request
///   2. Server responds with capabilities
///   3. Client sends `notifications/initialized`
///   4. Server begins processing `tools/list`, `tools/call` etc.
///   5. Loop until EOF or Ctrl+C
///
/// ## Differences from `listen` mode
///
/// - Uses standard JSON-RPC 2.0 envelope (jsonrpc, id, method/params, result/error)
/// - Actions are exposed as MCP tools with JSON Schema input definitions
/// - Supports tool discovery via `tools/list`
/// - Standardized error codes (JSON-RPC 2.0 spec)
///
/// ## Differences from MCP full spec
///
/// - No prompts or resources support (browser automation only)
/// - Pagination not implemented (tool list is small enough)
/// - No `listChanged` notifications (tool set is static at build time)

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};

use crate::browser::{set_media_blocking_status, BrowserState};
use crate::prompt;
use crate::utils::write_json_stdout;
use crate::{resolve_browser, shutdown_browser};

// ═══════════════════════════════════════════════════════════════════════
// Constants
// ═══════════════════════════════════════════════════════════════════════

/// MCP protocol version we advertise.
const MCP_PROTOCOL_VERSION: &str = "2025-03-26";

/// Max stdin line size (1 MB, matching listen mode).
const MAX_LINE_BYTES: usize = 1024 * 1024;

// ═══════════════════════════════════════════════════════════════════════
// JSON-RPC 2.0 error codes
// ═══════════════════════════════════════════════════════════════════════

const CODE_PARSE_ERROR: i32 = -32700;
const CODE_INVALID_REQUEST: i32 = -32600;
const CODE_METHOD_NOT_FOUND: i32 = -32601;
const CODE_INVALID_PARAMS: i32 = -32602;
const CODE_INTERNAL_ERROR: i32 = -32603;

/// Server-defined error codes (MCP reserves -32000..-32099).
const CODE_TOOL_EXECUTION_ERROR: i32 = -32002;

// ═══════════════════════════════════════════════════════════════════════
// JSON-RPC 2.0 message types
// ═══════════════════════════════════════════════════════════════════════

#[derive(Deserialize, Debug)]
struct JsonRpcRequest {
    jsonrpc: String,
    #[serde(default)]
    id: Option<Value>,
    method: String,
    #[serde(default)]
    params: Option<Value>,
}

#[derive(Serialize, Debug)]
struct JsonRpcResponse {
    jsonrpc: String,
    id: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<JsonRpcError>,
}

#[derive(Serialize, Debug)]
struct JsonRpcError {
    code: i32,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<Value>,
}

// ═══════════════════════════════════════════════════════════════════════
// MCP-specific types
// ═══════════════════════════════════════════════════════════════════════

#[derive(Serialize, Debug)]
struct McpTool {
    name: String,
    description: String,
    #[serde(rename = "inputSchema")]
    input_schema: Value,
}

#[derive(Serialize, Debug)]
struct McpContent {
    #[serde(rename = "type")]
    content_type: String,
    text: String,
}

#[derive(Serialize, Debug)]
struct McpCallResult {
    content: Vec<McpContent>,
}

// ═══════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════

fn rpc_error(code: i32, message: impl Into<String>) -> JsonRpcError {
    JsonRpcError {
        code,
        message: message.into(),
        data: None,
    }
}

fn rpc_id(id: &Option<Value>) -> Value {
    id.clone().unwrap_or(Value::Null)
}

fn error_response(id: Value, code: i32, message: impl Into<String>) -> Value {
    json!(JsonRpcResponse {
        jsonrpc: "2.0".into(),
        id,
        result: None,
        error: Some(rpc_error(code, message)),
    })
}

fn success_response(id: Value, result: Value) -> Value {
    json!(JsonRpcResponse {
        jsonrpc: "2.0".into(),
        id,
        result: Some(result),
        error: None,
    })
}

// ═══════════════════════════════════════════════════════════════════════
// Tool definitions
// ═══════════════════════════════════════════════════════════════════════

fn tool_definitions() -> Vec<McpTool> {
    vec![
        McpTool {
            name: "navigate".into(),
            description: "Navigate the browser to a URL and return the interactive element tree with page metadata.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to navigate to"
                    }
                },
                "required": ["url"]
            }),
        },
        McpTool {
            name: "click".into(),
            description: "Click an interactive element identified by its target ID (e.g. 'e5', 'e12') and return the updated element tree.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The element ID to click, as shown in the tree output (e.g. e5, e12)"
                    }
                },
                "required": ["target_id"]
            }),
        },
        McpTool {
            name: "type".into(),
            description: "Type text into an input element identified by its target ID. Uses human-like keystroke simulation with randomized delays.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "The element ID to type into (e.g. e3)"
                    },
                    "text": {
                        "type": "string",
                        "description": "The text to type into the element"
                    }
                },
                "required": ["target_id", "text"]
            }),
        },
        McpTool {
            name: "screenshot".into(),
            description: "Capture a screenshot of the current page. Returns the file path to the saved PNG image.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {}
            }),
        },
        McpTool {
            name: "tree".into(),
            description: "Extract the interactive element tree of the current page. Each line shows an element with its ID, role, and text content.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {}
            }),
        },
        McpTool {
            name: "meta".into(),
            description: "Get page metadata: title, URL, and interactive element count.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {}
            }),
        },
        McpTool {
            name: "get_prompt".into(),
            description: "Get the built-in AI agent system prompt. This prompt instructs an LLM on how to use the browser automation tools.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {}
            }),
        },
        McpTool {
            name: "configure".into(),
            description: "Configure resource loading behavior at runtime. When media_enabled is true, all resources (images, CSS, fonts) are loaded for full page rendering. When false, non-essential resources are blocked for faster page loads.".into(),
            input_schema: json!({
                "type": "object",
                "properties": {
                    "media_enabled": {
                        "type": "boolean",
                        "description": "If true, allow all resources (images, CSS, fonts). If false, block non-essential resources."
                    }
                },
                "required": ["media_enabled"]
            }),
        },
    ]
}

// ═══════════════════════════════════════════════════════════════════════
// Entry point
// ═══════════════════════════════════════════════════════════════════════

pub async fn cmd_mcp(
    profile: Option<&str>,
    connect: Option<&str>,
    show: bool,
) -> i32 {
    let bs = match resolve_browser(profile, connect, show).await {
        Ok(bs) => bs,
        Err(e) => {
            let _ = write_json_stdout(&error_response(
                Value::Null,
                CODE_INTERNAL_ERROR,
                format!("{:#}", e),
            ))
            .await;
            return 1;
        }
    };

    // Enable resource blocking by default for fast page loads
    if let Err(e) = bs.enable_resource_blocking().await {
        let _ = write_json_stdout(&error_response(
            Value::Null,
            CODE_TOOL_EXECUTION_ERROR,
            format!("{:#}", e),
        ))
        .await;
        shutdown_browser(bs, "mcp").await;
        return 1;
    }

    let exit_code = run_mcp_loop(&bs).await;
    shutdown_browser(bs, "mcp").await;
    exit_code
}

// ═══════════════════════════════════════════════════════════════════════
// MCP message loop
// ═══════════════════════════════════════════════════════════════════════

async fn run_mcp_loop(bs: &BrowserState) -> i32 {
    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin);
    let mut line_buf = String::new();
    let mut initialized = false;
    let mut media_on = false;

    loop {
        tokio::select! {
            biased;

            read_result = reader.read_line(&mut line_buf) => {
                match read_result {
                    Ok(0) => {
                        eprintln!("[mcp] Stdin closed. Shutting down...");
                        return 0;
                    }
                    Ok(_n) => {
                        if line_buf.len() > MAX_LINE_BYTES {
                            eprintln!(
                                "[mcp] Line too large ({} bytes, max {}), discarding",
                                line_buf.len(), MAX_LINE_BYTES
                            );
                            line_buf.clear();
                            continue;
                        }

                        // Keep buffering until we see a newline
                        if !line_buf.ends_with('\n') && !line_buf.ends_with('\r') {
                            continue;
                        }

                        let trimmed = line_buf.trim().to_string();
                        line_buf.clear();

                        if trimmed.is_empty() {
                            continue;
                        }

                        if !initialized {
                            // ── Pre-initialization phase ──
                            // Server waits for `initialize`, responds, then waits for
                            // `notifications/initialized` from the client before accepting
                            // `tools/list` and `tools/call`.
                            let req: JsonRpcRequest = match serde_json::from_str(&trimmed) {
                                Ok(r) => r,
                                Err(e) => {
                                    let resp = error_response(
                                        Value::Null,
                                        CODE_PARSE_ERROR,
                                        format!("Parse error: {}", e),
                                    );
                                    if write_json_stdout(&resp).await.is_err() {
                                        return 1;
                                    }
                                    continue;
                                }
                            };

                            if req.method == "initialize" {
                                if req.jsonrpc != "2.0" {
                                    let resp = error_response(
                                        rpc_id(&req.id),
                                        CODE_INVALID_REQUEST,
                                        "jsonrpc must be '2.0'",
                                    );
                                    if write_json_stdout(&resp).await.is_err() {
                                        return 1;
                                    }
                                    continue;
                                }

                                // Build and send initialize response
                                let resp = build_initialize_response(&req);
                                if write_json_stdout(&resp).await.is_err() {
                                    return 1;
                                }
                                // Awaiting notifications/initialized from client
                                // (initialized remains false)
                                continue;
                            }

                            if req.method == "notifications/initialized" {
                                // Client is done with init — enter operational phase
                                initialized = true;
                                // This is a notification — no response expected
                                continue;
                            }

                            // Any other method before init — reject
                            let resp = if req.jsonrpc == "2.0" {
                                error_response(
                                    rpc_id(&req.id),
                                    CODE_INVALID_REQUEST,
                                    "Not initialized. Send 'initialize' first.",
                                )
                            } else {
                                error_response(
                                    Value::Null,
                                    CODE_INVALID_REQUEST,
                                    "Not initialized. Send 'initialize' first.",
                                )
                            };
                            if write_json_stdout(&resp).await.is_err() {
                                return 1;
                            }
                        } else {
                            // ── Normal request-response cycle ──
                            let response = handle_message(&bs, &trimmed, &mut media_on).await;
                            if write_json_stdout(&response).await.is_err() {
                                eprintln!("[mcp] Stdout write failed. Shutting down...");
                                return 1;
                            }
                        }
                    }
                    Err(e) => {
                        eprintln!("[mcp] Stdin read error: {}", e);
                        return 1;
                    }
                }
            }
            _ = tokio::signal::ctrl_c() => {
                eprintln!("[mcp] Ctrl+C received. Shutting down...");
                return 0;
            }
        }
    }
}

/// Build the MCP initialize response.
fn build_initialize_response(req: &JsonRpcRequest) -> Value {
    success_response(
        rpc_id(&req.id),
        json!({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "agent-browser-cli",
                "version": env!("CARGO_PKG_VERSION")
            }
        }),
    )
}

/// Handle a single JSON-RPC message after initialization.
async fn handle_message(
    bs: &BrowserState,
    line: &str,
    media_on: &mut bool,
) -> Value {
    let req: JsonRpcRequest = match serde_json::from_str(line) {
        Ok(r) => r,
        Err(e) => {
            return error_response(Value::Null, CODE_PARSE_ERROR, format!("Parse error: {}", e));
        }
    };

    if req.jsonrpc != "2.0" {
        return error_response(rpc_id(&req.id), CODE_INVALID_REQUEST, "jsonrpc must be '2.0'");
    }

    match req.method.as_str() {
        "tools/list" => handle_tools_list(&req),
        "tools/call" => handle_tools_call(bs, &req, media_on).await,
        _ => error_response(
            rpc_id(&req.id),
            CODE_METHOD_NOT_FOUND,
            format!("Method not found: {}", req.method),
        ),
    }
}

// ═══════════════════════════════════════════════════════════════════════
// tools/list handler
// ═══════════════════════════════════════════════════════════════════════

fn handle_tools_list(req: &JsonRpcRequest) -> Value {
    success_response(
        rpc_id(&req.id),
        json!({
            "tools": tool_definitions()
        }),
    )
}

// ═══════════════════════════════════════════════════════════════════════
// tools/call handlers
// ═══════════════════════════════════════════════════════════════════════

async fn handle_tools_call(
    bs: &BrowserState,
    req: &JsonRpcRequest,
    media_on: &mut bool,
) -> Value {
    let params = match &req.params {
        Some(p) => p,
        None => {
            return error_response(rpc_id(&req.id), CODE_INVALID_PARAMS, "Missing params");
        }
    };

    let tool_name = match params.get("name").and_then(|v| v.as_str()) {
        Some(n) => n,
        None => {
            return error_response(rpc_id(&req.id), CODE_INVALID_PARAMS, "Missing 'name' in params");
        }
    };

    let args = params.get("arguments").and_then(|v| v.as_object()).cloned().unwrap_or_default();

    let text_content = match tool_name {
        "navigate" => handle_navigate(bs, &args).await,
        "click" => handle_click(bs, &args).await,
        "type" => handle_type(bs, &args).await,
        "screenshot" => handle_screenshot(bs).await,
        "tree" => handle_tree(bs).await,
        "meta" => handle_meta(bs).await,
        "get_prompt" => Ok(prompt::AGENT_SYSTEM_PROMPT.to_string()),
        "configure" => handle_configure(bs, &args, media_on).await,
        _ => Err(format!("Unknown tool: '{}'", tool_name)),
    };

    match text_content {
        Ok(text) => success_response(
            rpc_id(&req.id),
            json!(McpCallResult {
                content: vec![McpContent {
                    content_type: "text".into(),
                    text,
                }],
            }),
        ),
        Err(err) => error_response(rpc_id(&req.id), CODE_TOOL_EXECUTION_ERROR, err),
    }
}

// ─── Tool implementation helpers ──────────────────────────────────────

async fn handle_navigate(bs: &BrowserState, args: &serde_json::Map<String, Value>) -> Result<String, String> {
    let url = args
        .get("url")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'url' argument for navigate".to_string())?;

    bs.navigate(url)
        .await
        .map_err(|e| format!("{:#}", e))?;

    let tree = bs
        .extract_tree()
        .await
        .map_err(|e| format!("{:#}", e))?;

    let meta = bs
        .get_page_meta()
        .await
        .map_err(|e| format!("{:#}", e))?;

    let title = meta.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let interactive_count = meta
        .get("interactiveCount")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    Ok(format!(
        "Title: {}\nURL: {}\nInteractive elements: {}\n\nTree:\n{}",
        title, url, interactive_count, tree
    ))
}

async fn handle_click(bs: &BrowserState, args: &serde_json::Map<String, Value>) -> Result<String, String> {
    let target_id = args
        .get("target_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'target_id' argument for click".to_string())?;

    let (_scrolled, tree) = bs
        .click(target_id)
        .await
        .map_err(|e| format!("{:#}", e))?;

    Ok(tree)
}

async fn handle_type(bs: &BrowserState, args: &serde_json::Map<String, Value>) -> Result<String, String> {
    let target_id = args
        .get("target_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'target_id' argument for type".to_string())?;

    let text = args
        .get("text")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'text' argument for type".to_string())?;

    let tree = bs
        .type_text(target_id, text)
        .await
        .map_err(|e| format!("{:#}", e))?;

    Ok(tree)
}

async fn handle_screenshot(bs: &BrowserState) -> Result<String, String> {
    let path = bs
        .screenshot()
        .await
        .map_err(|e| format!("{:#}", e))?;

    Ok(format!("Screenshot saved to: {}", path))
}

async fn handle_tree(bs: &BrowserState) -> Result<String, String> {
    bs.extract_tree()
        .await
        .map_err(|e| format!("{:#}", e))
}

async fn handle_meta(bs: &BrowserState) -> Result<String, String> {
    let meta = bs
        .get_page_meta()
        .await
        .map_err(|e| format!("{:#}", e))?;

    let title = meta.get("title").and_then(|v| v.as_str()).unwrap_or("");
    let url = meta.get("url").and_then(|v| v.as_str()).unwrap_or("");
    let count = meta
        .get("interactiveCount")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);

    Ok(format!("Title: {}\nURL: {}\nInteractive elements: {}", title, url, count))
}

async fn handle_configure(
    bs: &BrowserState,
    args: &serde_json::Map<String, Value>,
    media_on: &mut bool,
) -> Result<String, String> {
    let enabled = args
        .get("media_enabled")
        .and_then(|v| v.as_bool())
        .ok_or_else(|| "Missing 'media_enabled' boolean argument for configure".to_string())?;

    let page_handle = {
        let guard = crate::browser::lock_with_timeout(&bs.page, "page")
            .await
            .map_err(|e| format!("{:#}", e))?;
        guard.clone()
    };

    set_media_blocking_status(&page_handle, enabled)
        .await
        .map_err(|e| format!("{:#}", e))?;

    *media_on = enabled;

    Ok(format!("Resource blocking: {}", if enabled { "disabled (all resources allowed)" } else { "enabled (blocking images, CSS, fonts)" }))
}
