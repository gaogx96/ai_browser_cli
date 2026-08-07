mod bridge;
mod browser;
mod facade;
mod injector;
mod prompt;
mod session_router;
mod utils;

use std::time::Duration;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use serde_json::json;
use tokio::io::{AsyncBufReadExt, BufReader};

use browser::{BrowserState, set_media_blocking_status};
use utils::write_json_stdout;

/// AI Agent Browser CLI - High-performance headless browser control via CDP.
#[derive(Parser)]
#[command(name = "agent-browser-cli")]
#[command(version = "0.6.0")]
#[command(about = "AI Agent headless browser CLI", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// One-shot page load: navigate, extract AI tree, then exit.
    View {
        /// URL to load
        #[arg(short, long)]
        url: String,

        /// Chrome profile path for session reuse
        #[arg(short, long)]
        profile: Option<String>,

        /// Connect to an existing Chrome instance via debugging port.
        /// E.g., --connect http://127.0.0.1:9222
        /// Chrome must be running with --remote-debugging-port=9222.
        #[arg(long)]
        connect: Option<String>,

        /// Show browser window (disable headless mode)
        #[arg(long, default_value = "false")]
        show: bool,

        /// Use extension bridge mode (no --remote-debugging-port needed).
        /// Requires the Agent Browser Bridge extension to be loaded in Chrome.
        #[arg(long, default_value = "false")]
        extension: bool,
    },

    /// Persistent pipe mode: listen on stdin for JSON commands, respond on stdout.
    Listen {
        /// Chrome profile path for session reuse
        #[arg(short, long)]
        profile: Option<String>,

        /// Connect to an existing Chrome instance via debugging port.
        /// E.g., --connect http://127.0.0.1:9222
        #[arg(long)]
        connect: Option<String>,

        /// Resource loading strategy:
        ///   block — block images, CSS, fonts, ads (fastest, default)
        ///   allow — allow all resources (full rendering)
        ///   smart — block only ads/tracking, allow images and CSS
        #[arg(long, default_value = "block")]
        resources: String,

        /// Show browser window (disable headless mode)
        #[arg(long, default_value = "false")]
        show: bool,

        /// Use extension bridge mode (no --remote-debugging-port needed).
        /// Requires the Agent Browser Bridge extension to be loaded in Chrome.
        #[arg(long, default_value = "false")]
        extension: bool,
    },

    /// Print the AI Agent system prompt to stdout and exit.
    /// Use this to feed the prompt into your LLM pipeline.
    Prompt,

    /// Test the Chrome extension bridge: connect to extension, get active tab, evaluate JS.
    /// This is a development tool to verify the extension ↔ bridge communication.
    BridgeTest,

    /// Test download through the extension bridge.
    DownloadTest,

    /// Test the full CDP façade: connect chromiumoxide through the extension bridge.
    /// Runs connect → new_page → goto → evaluate → screenshot → pages.
    /// This is a development tool to verify the extension ↔ bridge ↔ facade pipeline.
    ExtensionConnectTest,

    /// Test OOPIF extraction through the extension bridge.
    /// Runs connect → new_page → goto <url> → extract_tree → print results.
    /// Default URL is the OOPIF test page on localhost:8080.
    ExtensionTreeTest {
        /// URL to load (default: http://127.0.0.1:8080/oopif_main.html)
        #[arg(default_value = "http://127.0.0.1:8080/oopif_main.html")]
        url: String,
    },

    /// List all tabs in the browser via the extension bridge.
    ListTabs,
}

const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_STDIN_LINE_BYTES: usize = 1024 * 1024;

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    let exit_code = match cli.command {
        Commands::Prompt => {
            println!("{}", prompt::AGENT_SYSTEM_PROMPT);
            0
        }
        Commands::View { url, profile, connect, show, extension } => {
            if extension {
                cmd_view_extension(&url).await
            } else {
                cmd_view(&url, profile.as_deref(), connect.as_deref(), show).await
            }
        }
        Commands::Listen {
            profile,
            connect,
            resources,
            show,
            extension,
        } => {
            if extension {
                cmd_listen_extension(&resources).await
            } else {
                cmd_listen(
                    profile.as_deref(),
                    connect.as_deref(),
                    &resources,
                    show,
                )
                .await
            }
        }
        Commands::BridgeTest => cmd_bridge_test().await,
        Commands::DownloadTest => cmd_download_test().await,
        Commands::ListTabs => cmd_list_tabs().await,
        Commands::ExtensionConnectTest => cmd_extension_connect_test().await,
        Commands::ExtensionTreeTest { url } => cmd_extension_tree_test(&url).await,
    };

    std::process::exit(exit_code);
}

async fn shutdown_browser(mut bs: BrowserState, context: &str) {
    match tokio::time::timeout(SHUTDOWN_TIMEOUT, bs.close()).await {
        Ok(Ok(())) => {}
        Ok(Err(e)) => {
            eprintln!("[{}] Browser close error: {}", context, e);
        }
        Err(_) => {
            eprintln!(
                "[{}] Browser close timed out ({}s)",
                context,
                SHUTDOWN_TIMEOUT.as_secs()
            );
        }
    }
}

/// Launch or connect to a browser based on the provided arguments.
///
/// Auto-detection logic:
///   1. If --connect is explicitly provided, use that URL directly.
///   2. Otherwise, probe 127.0.0.1:9222 with a 50ms TCP handshake.
///      - If Chrome responds → auto-attach in connect mode (preserves sessions).
///      - If no response → fall back to launch mode with anti-detection flags.
async fn resolve_browser(
    profile: Option<&str>,
    connect: Option<&str>,
    show: bool,
) -> Result<BrowserState> {
    // Case 1: explicit --connect flag
    if let Some(url) = connect {
        eprintln!("[connect] Connecting to existing Chrome at {}...", url);
        return BrowserState::connect(url).await;
    }

    // Case 2: auto-detect Chrome on 9222 (50ms probe)
    let probe = tokio::time::timeout(
        Duration::from_millis(50),
        tokio::net::TcpStream::connect("127.0.0.1:9222"),
    )
    .await;

    match probe {
        Ok(Ok(_stream)) => {
            // Port 9222 is open — a Chrome instance with remote debugging exists.
            eprintln!("[Auto-Detect] Found active Chrome on port 9222. Attaching seamlessly...");
            return BrowserState::connect("http://127.0.0.1:9222").await;
        }
        _ => {
            // No Chrome on 9222 — fall through to launch mode.
        }
    }

    // Case 3: fallback — launch a new Chrome with anti-detection hardening.
    eprintln!("[Launch] No Chrome detected on port 9222. Starting hardened headless instance...");
    BrowserState::launch(profile, show).await
}

async fn cmd_view(
    url: &str,
    profile: Option<&str>,
    connect: Option<&str>,
    show: bool,
) -> i32 {
    let bs = match resolve_browser(profile, connect, show).await {
        Ok(bs) => bs,
        Err(e) => {
            let _ = write_json_stdout(&json!({
                "status": "error",
                "error": format!("{:#}", e)
            }))
            .await;
            return 1;
        }
    };

    if let Err(e) = bs.enable_resource_blocking().await {
        let _ = write_json_stdout(&json!({
            "status": "error",
            "error": format!("{:#}", e)
        }))
        .await;
        shutdown_browser(bs, "view").await;
        return 1;
    }

    if let Err(e) = bs.navigate(url).await {
        let _ = write_json_stdout(&json!({
            "status": "error",
            "error": format!("{:#}", e)
        }))
        .await;
        shutdown_browser(bs, "view").await;
        return 1;
    }

    let (tree, meta) = match (bs.extract_tree().await, bs.get_page_meta().await) {
        (Ok(tree), Ok(meta)) => (tree, meta),
        (Err(e), _) | (_, Err(e)) => {
            let _ = write_json_stdout(&json!({
                "status": "error",
                "error": format!("{:#}", e)
            }))
            .await;
            shutdown_browser(bs, "view").await;
            return 1;
        }
    };

    let output = json!({
        "status": "ok",
        "url": url,
        "title": meta.get("title").and_then(|v| v.as_str()).unwrap_or(""),
        "interactive_count": meta.get("interactiveCount").and_then(|v| v.as_i64()).unwrap_or(0),
        "tree": tree,
    });

    let write_ok = write_json_stdout(&output).await.is_ok();
    shutdown_browser(bs, "view").await;
    if write_ok { 0 } else { 1 }
}

/// Extension mode: view command.
/// Starts bridge + facade, connects chromiumoxide through the extension,
/// navigates, extracts tree, outputs JSON, then exits.
async fn cmd_view_extension(url: &str) -> i32 {
    use std::sync::Arc;

    let (event_tx, mut event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[extension] Bridge error: {:#}", e);
            return 1;
        }
    };

    if let Err(e) = bridge.wait_for_extension().await {
        eprintln!("[extension] Extension not connected: {:#}", e);
        eprintln!("[extension] Make sure the Agent Browser Bridge extension is loaded in Chrome.");
        return 1;
    }

    let (placeholder_tx, _) = tokio::sync::mpsc::unbounded_channel();
    let router = Arc::new(session_router::SessionRouter::new(bridge.clone(), placeholder_tx));
    let router_for_events = router.clone();
    tokio::spawn(async move {
        while let Some(event) = event_rx.recv().await {
            router_for_events.handle_extension_event(event).await;
        }
    });

    let facade = match facade::start_facade(router.clone()).await {
        Ok(f) => f,
        Err(e) => { eprintln!("[extension] Facade error: {:#}", e); return 1; }
    };
    router.set_facade_tx(facade.facade_tx.clone()).await;
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;

    let bs = match BrowserState::connect("ws://127.0.0.1:9224/devtools/browser/agent").await {
        Ok(bs) => bs,
        Err(e) => {
            let _ = write_json_stdout(&json!({"status": "error", "error": format!("{:#}", e)})).await;
            return 1;
        }
    };

    if let Err(e) = bs.navigate(url).await {
        let _ = write_json_stdout(&json!({"status": "error", "error": format!("{:#}", e)})).await;
        return 1;
    }

    let (tree, meta) = match (bs.extract_tree().await, bs.get_page_meta().await) {
        (Ok(tree), Ok(meta)) => (tree, meta),
        (Err(e), _) | (_, Err(e)) => {
            let _ = write_json_stdout(&json!({"status": "error", "error": format!("{:#}", e)})).await;
            return 1;
        }
    };

    let output = json!({
        "status": "ok",
        "url": url,
        "title": meta.get("title").and_then(|v| v.as_str()).unwrap_or(""),
        "interactive_count": meta.get("interactiveCount").and_then(|v| v.as_i64()).unwrap_or(0),
        "tree": tree,
    });

    let _ = write_json_stdout(&output).await;
    // In extension mode, don't close the browser — just disconnect.
    0
}

async fn cmd_listen(
    profile: Option<&str>,
    connect: Option<&str>,
    resources: &str,
    show: bool,
) -> i32 {
    let bs = match resolve_browser(profile, connect, show).await {
        Ok(bs) => bs,
        Err(e) => {
            let _ = write_json_stdout(&json!({
                "status": "error",
                "error": format!("{:#}", e)
            }))
            .await;
            return 1;
        }
    };

    // Apply resource loading strategy
    let mut media_on = match resources {
        "block" => {
            // Block images, CSS, fonts, ads — fastest page loads
            if let Err(e) = bs.enable_resource_blocking().await {
                let _ = write_json_stdout(&json!({
                    "status": "error",
                    "error": format!("{:#}", e)
                })).await;
                shutdown_browser(bs, "listen").await;
                return 1;
            }
            false
        }
        "allow" => {
            // Allow all resources — full rendering
            true
        }
        "smart" => {
            // Block ads/tracking only, allow images/CSS/fonts
            // Uses CDP Network.setBlockedURLs with ad-only patterns
            if let Err(e) = bs.enable_smart_blocking().await {
                let _ = write_json_stdout(&json!({
                    "status": "error",
                    "error": format!("{:#}", e)
                })).await;
                shutdown_browser(bs, "listen").await;
                return 1;
            }
            false
        }
        _ => {
            let _ = write_json_stdout(&json!({
                "status": "error",
                "error": format!("Unknown resources strategy: '{}'. Use block/allow/smart", resources)
            })).await;
            shutdown_browser(bs, "listen").await;
            return 1;
        }
    };

    let ready = json!({
        "status": "ready",
        "media_enabled": media_on,
        "message": "agent-browser-cli is listening"
    });
    if write_json_stdout(&ready).await.is_err() {
        shutdown_browser(bs, "listen").await;
        return 1;
    }

    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin);
    let mut line_buf = String::new();

    let exit_code = loop {
        // C-10 fix: do NOT clear at the top of the loop.
        // read_line() appends to the buffer. If a partial read occurs
        // (no newline found), the accumulated data must be preserved
        // for the next read_line() call. The buffer is only cleared
        // AFTER a complete line is processed (see end of arm).

        tokio::select! {
            biased;

            read_result = reader.read_line(&mut line_buf) => {
                match read_result {
                    Ok(0) => {
                        eprintln!("[listen] Stdin closed. Shutting down...");
                        break 0;
                    }
                    Ok(_n) => {
                        // C-10 fix: check accumulated buffer size,
                        // not the bytes read in this single call.
                        if line_buf.len() > MAX_STDIN_LINE_BYTES {
                            eprintln!(
                                "[stdin] Line too large ({} bytes, max {}), discarding",
                                line_buf.len(), MAX_STDIN_LINE_BYTES
                            );
                            line_buf.clear();
                            continue;
                        }

                        // C-10 fix: if no newline, this is a partial read.
                        // Keep the data in the buffer; next read_line() will
                        // append more data until a newline is found.
                        if !line_buf.ends_with('\n') && !line_buf.ends_with('\r') {
                            continue;
                        }

                        let trimmed = line_buf.trim().to_string();
                        // C-10 fix: clear AFTER extracting the complete line.
                        line_buf.clear();

                        if trimmed.is_empty() {
                            continue;
                        }

                        let response = process_command(&bs, &trimmed, &mut media_on).await;
                        let resp_json = match response {
                            Ok(data) => data,
                            Err(e) => json!({
                                "status": "error",
                                "error": format!("{:#}", e)
                            }),
                        };

                        if write_json_stdout(&resp_json).await.is_err() {
                            eprintln!("[listen] Stdout write failed. Shutting down...");
                            break 1;
                        }
                    }
                    Err(e) => {
                        eprintln!("[stdin] Read error: {}", e);
                        break 1;
                    }
                }
            }
            _ = tokio::signal::ctrl_c() => {
                eprintln!("[listen] Ctrl+C received. Shutting down...");
                break 0;
            }
        }
    };

    shutdown_browser(bs, "listen").await;
    exit_code
}

/// Extension mode: listen command.
/// Starts bridge + facade, connects chromiumoxide through the extension,
/// then enters the same stdin/stdout JSON command loop as cmd_listen.
async fn cmd_listen_extension(resources: &str) -> i32 {
    use std::sync::Arc;

    let (event_tx, mut event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => { eprintln!("[extension] Bridge error: {:#}", e); return 1; }
    };

    if let Err(e) = bridge.wait_for_extension().await {
        eprintln!("[extension] Extension not connected: {:#}", e);
        eprintln!("[extension] Make sure the Agent Browser Bridge extension is loaded in Chrome.");
        return 1;
    }

    let (placeholder_tx, _) = tokio::sync::mpsc::unbounded_channel();
    let router = Arc::new(session_router::SessionRouter::new(bridge.clone(), placeholder_tx));
    let router_for_events = router.clone();
    tokio::spawn(async move {
        while let Some(event) = event_rx.recv().await {
            router_for_events.handle_extension_event(event).await;
        }
    });

    let facade = match facade::start_facade(router.clone()).await {
        Ok(f) => f,
        Err(e) => { eprintln!("[extension] Facade error: {:#}", e); return 1; }
    };
    router.set_facade_tx(facade.facade_tx.clone()).await;
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;

    // 兜底恢复焦点：connect 前记录原活动标签页（通过扩展查询）
    let original_tab_id: Option<i64> = {
        match bridge.send_to_extension(json!({"op": "getActiveTab"})).await {
            Ok(v) => v.get("tabId").and_then(|x| x.as_i64()),
            Err(_) => None,
        }
    };
    if let Some(tid) = original_tab_id {
        eprintln!("[tabs] Original active tab: {}", tid);
    }

    let bs = match BrowserState::connect("ws://127.0.0.1:9224/devtools/browser/agent").await {
        Ok(bs) => bs,
        Err(e) => {
            let _ = write_json_stdout(&json!({"status": "error", "error": format!("{:#}", e)})).await;
            return 1;
        }
    };

    // 兜底：connect 后如果 agent 标签页意外抢到焦点，且原标签页仍存在，则恢复原标签页
    // 环境变量 AGENT_BROWSER_RESTORE_FOCUS=0 可禁用此行为
    if std::env::var("AGENT_BROWSER_RESTORE_FOCUS").as_deref() != Ok("0") {
        if let Some(orig_id) = original_tab_id {
            // 查询当前活动标签页
            let current_active: Option<i64> = bridge
                .send_to_extension(json!({"op": "getActiveTab"}))
                .await
                .ok()
                .and_then(|v| v.get("tabId").and_then(|x| x.as_i64()));

            // 仅当"当前活动页是 agent 页（即与原始活动页不同）"时才检查原页是否存在
            if current_active.is_some() && current_active != Some(orig_id) {
                // 查询原标签页是否仍存在（通过 chrome.debugger.getTargets 的 tabId 字段判断）
                let orig_exists: bool = bridge
                    .send_to_extension(json!({"op": "getTargets"}))
                    .await
                    .map(|targets| {
                        targets
                            .get("targets")
                            .and_then(|t| t.as_array())
                            .map(|arr| {
                                arr.iter().any(|t| t.get("tabId").and_then(|x| x.as_i64()) == Some(orig_id))
                            })
                            .unwrap_or(false)
                    })
                    .unwrap_or(false);

                if orig_exists {
                    eprintln!("[tabs] Agent tab stole focus, restoring original tab {}", orig_id);
                    let _ = bridge
                        .send_to_extension(json!({"op": "activateTab", "tabId": orig_id}))
                        .await;
                }
            }
        }
    }

    // Apply resource strategy
    match resources {
        "block" => { let _ = bs.enable_resource_blocking().await; }
        "smart" => { let _ = bs.enable_smart_blocking().await; }
        _ => {}
    }

    let ready = json!({
        "status": "ready",
        "media_enabled": false,
        "message": "agent-browser-cli is listening (extension mode)"
    });
    if write_json_stdout(&ready).await.is_err() {
        return 1;
    }

    let stdin = tokio::io::stdin();
    let mut reader = BufReader::new(stdin);
    let mut line_buf = String::new();
    let mut media_on = false;

    let exit_code = loop {
        tokio::select! {
            biased;
            read_result = reader.read_line(&mut line_buf) => {
                match read_result {
                    Ok(0) => { eprintln!("[listen] Stdin closed. Shutting down..."); break 0; }
                    Ok(_n) => {
                        if line_buf.len() > 1024 * 1024 { line_buf.clear(); continue; }
                        if !line_buf.ends_with('\n') && !line_buf.ends_with('\r') { continue; }
                        let trimmed = line_buf.trim().to_string();
                        line_buf.clear();
                        if trimmed.is_empty() { continue; }
                        let response = process_command(&bs, &trimmed, &mut media_on).await;
                        let resp_json = match response {
                            Ok(data) => data,
                            Err(e) => json!({"status": "error", "error": format!("{:#}", e)}),
                        };
                        if write_json_stdout(&resp_json).await.is_err() { break 1; }
                    }
                    Err(e) => { eprintln!("[stdin] Read error: {}", e); break 1; }
                }
            }
            _ = tokio::signal::ctrl_c() => { eprintln!("[listen] Ctrl+C received. Shutting down..."); break 0; }
        }
    };

    // Don't close the browser in extension mode — just disconnect.
    eprintln!("[listen] Extension mode shutting down.");
    exit_code
}

/// Test the Chrome extension bridge.
///
/// 1. Start WebSocket server on 127.0.0.1:9223
/// 2. Wait for extension to connect
/// 3. Get active tab info
/// 4. Attach debugger to the active tab
/// 5. Evaluate document.title
/// 6. Print the result
/// 7. Detach and exit
async fn cmd_bridge_test() -> i32 {
    let (event_tx, _event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[bridge-test] Failed to start bridge: {:#}", e);
            return 1;
        }
    };

    // Wait for extension to connect and get active tab
    let tab_info = match bridge.wait_for_extension().await {
        Ok(info) => info,
        Err(e) => {
            eprintln!("[bridge-test] Failed to get active tab: {:#}", e);
            return 1;
        }
    };

    let tab_id = match tab_info.get("tabId").and_then(|v| v.as_i64()) {
        Some(id) => id,
        None => {
            eprintln!("[bridge-test] No tabId in response");
            return 1;
        }
    };

    let debuggee = serde_json::json!({"tabId": tab_id});

    // Step 1: Attach debugger
    eprintln!("[bridge-test] Attaching debugger to tab {}...", tab_id);
    let attach_cmd = serde_json::json!({"op": "attach", "debuggee": debuggee});
    match bridge.send_to_extension(attach_cmd).await {
        Ok(_) => eprintln!("[bridge-test] Attached successfully"),
        Err(e) => {
            // Check if already attached — this is not a fatal error
            let err_str = format!("{:#}", e);
            if err_str.contains("already attached") || err_str.contains("Another debugger") {
                eprintln!("[bridge-test] Debugger already attached to tab (continuing)");
            } else {
                eprintln!("[bridge-test] Attach failed: {:#}", e);
                return 1;
            }
        }
    }

    // Step 2: Evaluate document.title
    eprintln!("[bridge-test] Evaluating document.title...");
    let eval_cmd = serde_json::json!({
        "op": "sendCommand",
        "debuggee": {"tabId": tab_id},
        "method": "Runtime.evaluate",
        "params": {
            "expression": "document.title",
            "returnByValue": true
        }
    });

    match bridge.send_to_extension(eval_cmd).await {
        Ok(result) => {
            let title = result
                .pointer("/result/value")
                .and_then(|v| v.as_str())
                .unwrap_or("(no title)");
            println!("[bridge-test] ✅ Page title: {}", title);
            println!(
                "[bridge-test] Full result: {}",
                serde_json::to_string_pretty(&result).unwrap_or_default()
            );
        }
        Err(e) => {
            eprintln!("[bridge-test] Evaluate failed: {:#}", e);
            return 1;
        }
    }

    // Step 3: Detach
    eprintln!("[bridge-test] Detaching debugger...");
    let detach_cmd = serde_json::json!({"op": "detach", "debuggee": {"tabId": tab_id}});
    let _ = bridge.send_to_extension(detach_cmd).await;

    println!("[bridge-test] ✅ Bridge test completed successfully");
    0
}

/// Test download through the extension bridge.
async fn cmd_download_test() -> i32 {
    let (event_tx, _event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => { eprintln!("[download-test] Bridge error: {:#}", e); return 1; }
    };

    let tab_info = match bridge.wait_for_extension().await {
        Ok(info) => info,
        Err(e) => { eprintln!("[download-test] Extension not connected: {:#}", e); return 1; }
    };
    let tab_id = tab_info.get("tabId").and_then(|v| v.as_i64()).unwrap_or(0);
    eprintln!("[download-test] Active tab: {} (id={})", tab_info.get("title").and_then(|v| v.as_str()).unwrap_or("?"), tab_id);

    // Test download via chrome.downloads API
    // Use a public test file URL
    let test_url = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf";
    eprintln!("[download-test] Downloading from {}...", test_url);

    let download_cmd = serde_json::json!({
        "op": "download",
        "url": test_url,
        "filename": "test_download.pdf",
    });
    match bridge.send_to_extension(download_cmd).await {
        Ok(result) => {
            let state = result.get("state").and_then(|v| v.as_str()).unwrap_or("?");
            let filename = result.get("filename").and_then(|v| v.as_str()).unwrap_or("?");
            println!("[download-test] ✅ Download complete:");
            println!("  state: {}", state);
            println!("  filename: {}", filename);
        }
        Err(e) => {
            eprintln!("[download-test] ❌ Download failed: {:#}", e);
            return 1;
        }
    }

    0
}

/// List all tabs via the extension bridge.
/// Uses chrome.tabs.query({}) to enumerate all open tabs.
async fn cmd_list_tabs() -> i32 {
    let (event_tx, _event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => { eprintln!("[list-tabs] Bridge error: {:#}", e); return 1; }
    };

    let _ = match bridge.wait_for_extension().await {
        Ok(info) => info,
        Err(e) => { eprintln!("[list-tabs] Extension not connected: {:#}", e); return 1; }
    };

    // Output JSON array of page-type targets for easy script consumption
    match bridge.send_to_extension(serde_json::json!({"op": "getTargets"})).await {
        Ok(targets) => {
            let arr = targets.get("targets").and_then(|v| v.as_array()).unwrap();
            let pages: Vec<serde_json::Value> = arr.iter()
                .filter(|t| t.get("type").and_then(|v| v.as_str()) == Some("page"))
                .map(|t| {
                    serde_json::json!({
                        "id": t.get("id").and_then(|v| v.as_str()).unwrap_or(""),
                        "tabId": t.get("tabId").and_then(|v| v.as_i64()).unwrap_or(0),
                        "url": t.get("url").and_then(|v| v.as_str()).unwrap_or(""),
                        "attached": t.get("attached").and_then(|v| v.as_bool()).unwrap_or(false),
                    })
                })
                .collect();
            let output = serde_json::json!({"tabs": pages, "count": pages.len()});
            println!("{}", serde_json::to_string(&output).unwrap_or_default());
        }
        Err(e) => { eprintln!("[list-tabs] getTargets failed: {:#}", e); return 1; }
    }

    0
}

/// Test the full CDP façade through the extension bridge.
///
/// 1. Start bridge (WS 9223) + wait for extension
/// 2. Start façade (WS 9224) + SessionRouter
/// 3. Browser::connect("ws://127.0.0.1:9224/devtools/browser/agent")
/// 4. new_page → goto("https://example.com") → evaluate → screenshot → pages
/// 5. Print each step result
async fn cmd_extension_connect_test() -> i32 {
    use std::sync::Arc;
    use browser::BrowserState;

    // Step 1: Start bridge and wait for extension
    let (event_tx, mut event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => {
            eprintln!("[ect] Failed to start bridge: {:#}", e);
            return 1;
        }
    };

    match bridge.wait_for_extension().await {
        Ok(_) => eprintln!("[ect] ✅ Extension connected"),
        Err(e) => {
            eprintln!("[ect] Extension not connected: {:#}", e);
            return 1;
        }
    }

    // Step 2: Start facade + session router
    // Create the session router first with a placeholder, then wire it up after facade starts
    let (placeholder_tx, _) = tokio::sync::mpsc::unbounded_channel();
    let router = Arc::new(session_router::SessionRouter::new(bridge.clone(), placeholder_tx));

    let router_for_events = router.clone();
    tokio::spawn(async move {
        while let Some(event) = event_rx.recv().await {
            router_for_events.handle_extension_event(event).await;
        }
    });

    let facade = match facade::start_facade(router.clone()).await {
        Ok(f) => f,
        Err(e) => {
            eprintln!("[ect] Failed to start facade: {:#}", e);
            return 1;
        }
    };
    eprintln!("[ect] ✅ Facade started on ws://127.0.0.1:9224");

    // Wire the SessionRouter's facade_tx to the real facade channel
    router.set_facade_tx(facade.facade_tx.clone()).await;

    // Give the facade a moment to accept connections
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;

    // Step 3: Connect chromiumoxide through the facade
    eprintln!("[ect] Connecting chromiumoxide to facade...");
    let bs = match BrowserState::connect("ws://127.0.0.1:9224/devtools/browser/agent").await {
        Ok(bs) => bs,
        Err(e) => {
            eprintln!("[ect] ❌ Browser::connect failed: {:#}", e);
            return 1;
        }
    };
    println!("[ect] ✅ Browser::connect succeeded");

    // Step 4: Enable resource blocking (non-essential, but mirrors real usage)
    let _ = bs.enable_resource_blocking().await;

    // Step 5: Navigate to example.com
    eprintln!("[ect] Navigating to https://example.com...");
    match bs.navigate("https://example.com").await {
        Ok(_) => println!("[ect] ✅ Navigate succeeded"),
        Err(e) => {
            eprintln!("[ect] ❌ Navigate failed: {:#}", e);
            return 1;
        }
    }

    // Step 6: Evaluate document.title
    eprintln!("[ect] Evaluating document.title...");
    match bs.get_page_meta().await {
        Ok(meta) => {
            let title = meta.get("title").and_then(|v| v.as_str()).unwrap_or("(no title)");
            println!("[ect] ✅ Page title: {}", title);
        }
        Err(e) => {
            eprintln!("[ect] ❌ Evaluate failed: {:#}", e);
            return 1;
        }
    }

    // Step 7: Screenshot
    eprintln!("[ect] Taking screenshot...");
    match bs.screenshot().await {
        Ok(path) => println!("[ect] ✅ Screenshot saved to: {}", path),
        Err(e) => {
            eprintln!("[ect] ❌ Screenshot failed: {:#}", e);
            // Non-fatal, continue
        }
    }

    // Step 8: browser.pages()
    match bs.extract_tree().await {
        Ok(tree) => {
            let line_count = tree.lines().count();
            println!("[ect] ✅ Pages tree extracted ({} elements)", line_count);
        }
        Err(e) => {
            eprintln!("[ect] ❌ Pages failed: {:#}", e);
        }
    }

    println!("[ect] ✅ Extension connect test completed successfully");
    0
}

/// Test OOPIF extraction through the extension bridge.
/// Includes Phase 1.5 tests for chrome.debugger OOPIF capabilities.
async fn cmd_extension_tree_test(url: &str) -> i32 {
    use std::sync::Arc;
    use browser::BrowserState;

    eprintln!("[tree-test] Target URL: {}", url);

    // Step 1: Start bridge and wait for extension
    let (event_tx, mut event_rx) = tokio::sync::mpsc::unbounded_channel();
    let bridge = match bridge::start_bridge(event_tx).await {
        Ok(b) => b,
        Err(e) => { eprintln!("[tree-test] Bridge failed: {:#}", e); return 1; }
    };

    if let Err(e) = bridge.wait_for_extension().await {
        eprintln!("[tree-test] Extension not connected: {:#}", e);
        return 1;
    }

    // Step 2: Start facade + session router
    let (placeholder_tx, _) = tokio::sync::mpsc::unbounded_channel();
    let router = Arc::new(session_router::SessionRouter::new(bridge.clone(), placeholder_tx));

    let router_for_events = router.clone();
    tokio::spawn(async move {
        while let Some(event) = event_rx.recv().await {
            router_for_events.handle_extension_event(event).await;
        }
    });

    let facade = match facade::start_facade(router.clone()).await {
        Ok(f) => f,
        Err(e) => { eprintln!("[tree-test] Facade failed: {:#}", e); return 1; }
    };
    router.set_facade_tx(facade.facade_tx.clone()).await;

    tokio::time::sleep(std::time::Duration::from_millis(200)).await;

    // Step 3: Connect chromiumoxide
    eprintln!("[tree-test] Connecting chromiumoxide...");
    let bs = match BrowserState::connect("ws://127.0.0.1:9224/devtools/browser/agent").await {
        Ok(bs) => bs,
        Err(e) => { eprintln!("[tree-test] Connect failed: {:#}", e); return 1; }
    };
    println!("[tree-test] ✅ Browser::connect succeeded");

    // Step 4: Navigate to the test URL
    eprintln!("[tree-test] Navigating to {}...", url);
    if let Err(e) = bs.navigate(url).await {
        eprintln!("[tree-test] Navigate failed: {:#}", e);
        return 1;
    }
    println!("[tree-test] ✅ Navigate succeeded");

    // Step 5: Get page meta
    match bs.get_page_meta().await {
        Ok(meta) => {
            let title = meta.get("title").and_then(|v| v.as_str()).unwrap_or("(no title)");
            println!("[tree-test] Page title: {}", title);
        }
        Err(e) => eprintln!("[tree-test] Meta failed: {}", e),
    }

    // Step 5b: Test extract_tree with the new frame-based implementation
    println!("\n[tree-test] === Accessibility Tree (new extract_tree) ===");
    match bs.extract_tree().await {
        Ok(tree) => {
            println!("{}", tree);
            let line_count = tree.lines().filter(|l| !l.trim().is_empty()).count();
            println!("\n[tree-test] Total elements: {}", line_count);
        }
        Err(e) => eprintln!("[tree-test] Tree extraction failed: {}", e),
    }

    // Step 5c: Get the tabId of the page we navigated to
    println!("\n[tree-test] === Phase 1.5: Test A′ — getActiveTab ===");
    let tab_info = match bridge.send_to_extension(serde_json::json!({"op": "getActiveTab"})).await {
        Ok(info) => info,
        Err(e) => { eprintln!("[tree-test] getActiveTab failed: {}", e); return 1; }
    };
    let tab_id = tab_info.get("tabId").and_then(|v| v.as_i64()).unwrap_or(0);
    println!("[tree-test] Active tab: id={} title={:?}", tab_id, tab_info.get("title").and_then(|v| v.as_str()).unwrap_or(""));

    // Test A′: send Target.setAutoAttach and check for attachedToTarget events
    println!("\n[tree-test] === Test A′: Target.setAutoAttach on active tab ===");
    let sa_cmd = serde_json::json!({
        "op": "sendCommand",
        "debuggee": {"tabId": tab_id},
        "method": "Target.setAutoAttach",
        "params": {"autoAttach": true, "flatten": true, "waitForDebuggerOnStart": false},
    });
    match bridge.send_to_extension(sa_cmd).await {
        Ok(_) => println!("[tree-test] setAutoAttach sent successfully"),
        Err(e) => eprintln!("[tree-test] setAutoAttach failed: {}", e),
    }

    // Reload the page to trigger OOPIF discovery
    println!("[tree-test] Reloading page to trigger OOPIF events...");
    let reload_cmd = serde_json::json!({
        "op": "sendCommand",
        "debuggee": {"tabId": tab_id},
        "method": "Page.reload",
        "params": {},
    });
    let _ = bridge.send_to_extension(reload_cmd).await;
    tokio::time::sleep(std::time::Duration::from_secs(3)).await;

    // Wait for attachedToTarget events from the event stream
    // (We can't read from event_rx here because it's consumed by the router task)
    // Instead, let's check getTargets to see if new targets appeared
    println!("\n[tree-test] === Checking getTargets for OOPIF targets ===");
    match bridge.send_to_extension(serde_json::json!({"op": "getTargets"})).await {
        Ok(targets) => {
            let arr = targets.get("targets").and_then(|v| v.as_array()).unwrap();
            let oopif_candidates: Vec<_> = arr.iter().filter(|t| {
                let url = t.get("url").and_then(|v| v.as_str()).unwrap_or("");
                url.contains("8081") || t.get("type").and_then(|v| v.as_str()) == Some("iframe")
            }).collect();
            println!("[tree-test] OOPIF targets found: {}", oopif_candidates.len());
            for t in &oopif_candidates {
                println!("  id={} type={} url={}",
                    t.get("id").and_then(|v| v.as_str()).unwrap_or(""),
                    t.get("type").and_then(|v| v.as_str()).unwrap_or(""),
                    t.get("url").and_then(|v| v.as_str()).unwrap_or(""));
            }
            // Also show the total targets that are type=page with our URL
            let page_count = arr.iter().filter(|t| {
                t.get("type").and_then(|v| v.as_str()) == Some("page")
            }).count();
            println!("[tree-test] Total page targets: {}", page_count);
        }
        Err(e) => eprintln!("[tree-test] getTargets failed: {}", e),
    }

    // Test C: Page.getFrameTree → createIsolatedWorld → evaluate
    println!("\n[tree-test] === Test C: Page.getFrameTree + createIsolatedWorld ===");
    let ft_cmd = serde_json::json!({
        "op": "sendCommand",
        "debuggee": {"tabId": tab_id},
        "method": "Page.getFrameTree",
        "params": {},
    });
    match bridge.send_to_extension(ft_cmd).await {
        Ok(result) => {
            println!("[tree-test] Frame tree:");
            print_frame_tree(&result, 0);

            // Extract all frames from the frame tree
            let frames = collect_frames(&result);
            println!("\n[tree-test] Total frames: {}", frames.len());

            for (i, frame) in frames.iter().enumerate() {
                let frame_id = frame.get("id").or_else(|| frame.get("frameId")).and_then(|v| v.as_str()).unwrap_or("");
                let frame_url = frame.get("url").and_then(|v| v.as_str()).unwrap_or("");
                println!("\n  Frame {}: id={} url={}", i, frame_id, frame_url.chars().take(80).collect::<String>());

                // Try createIsolatedWorld on this frame
                let iw_cmd = serde_json::json!({
                    "op": "sendCommand",
                    "debuggee": {"tabId": tab_id},
                    "method": "Page.createIsolatedWorld",
                    "params": {"frameId": frame_id, "worldName": "agent_test", "grantUniveralAccess": true},
                });
                match bridge.send_to_extension(iw_cmd).await {
                    Ok(iw_result) => {
                        let ctx_id = iw_result.get("executionContextId").and_then(|v| v.as_i64()).unwrap_or(0);
                        println!("    createIsolatedWorld → executionContextId={}", ctx_id);

                        // Evaluate in the isolated world
                        let eval_cmd = serde_json::json!({
                            "op": "sendCommand",
                            "debuggee": {"tabId": tab_id},
                            "method": "Runtime.evaluate",
                            "params": {
                                "contextId": ctx_id,
                                "expression": "document.body?.innerText || document.body?.textContent || '(no body)'",
                                "returnByValue": true,
                            },
                        });
                        match bridge.send_to_extension(eval_cmd).await {
                            Ok(eval_result) => {
                                let text = eval_result.pointer("/result/value").and_then(|v| v.as_str()).unwrap_or("(no value)");
                                println!("    evaluate result (first 100 chars): {}", &text[..text.len().min(100)]);
                                if text.contains("IFRAME_MARKER") {
                                    println!("    ✅ IFRAME_MARKER found!");
                                } else if text.contains("MAIN_MARKER") {
                                    println!("    ⚠️ Contains MAIN_MARKER (not OOPIF)");
                                } else {
                                    println!("    - No marker found");
                                }
                            }
                            Err(e) => eprintln!("    evaluate failed: {}", e),
                        }

                        // Test marking and reading in the same world
                        let mark_cmd = serde_json::json!({
                            "op": "sendCommand",
                            "debuggee": {"tabId": tab_id},
                            "method": "Runtime.evaluate",
                            "params": {
                                "contextId": ctx_id,
                                "expression": "document.body?.setAttribute?.('data-agent-test', 't1') || 'no body'",
                                "returnByValue": true,
                            },
                        });
                        match bridge.send_to_extension(mark_cmd).await {
                            Ok(_) => {
                                let read_cmd = serde_json::json!({
                                    "op": "sendCommand",
                                    "debuggee": {"tabId": tab_id},
                                    "method": "Runtime.evaluate",
                                    "params": {
                                        "contextId": ctx_id,
                                        "expression": "document.querySelector('[data-agent-test=\"t1\"]') ? 'found' : 'not found'",
                                        "returnByValue": true,
                                    },
                                });
                                match bridge.send_to_extension(read_cmd).await {
                                    Ok(read_result) => {
                                        let found = read_result.pointer("/result/value").and_then(|v| v.as_str()).unwrap_or("?");
                                        println!("    Mark+read test: {}", found);
                                    }
                                    Err(e) => eprintln!("    mark+read failed: {}", e),
                                }
                            }
                            Err(e) => eprintln!("    mark failed: {}", e),
                        }
                    }
                    Err(e) => {
                        eprintln!("    createIsolatedWorld failed: {}", e);
                    }
                }
            }
        }
        Err(e) => eprintln!("[tree-test] getFrameTree failed: {}", e),
    }

    println!("\n[tree-test] ✅ Phase 1.5 tests completed");
    0
}

/// Recursively print the frame tree structure.
fn print_frame_tree(value: &serde_json::Value, depth: usize) {
    let indent = "  ".repeat(depth);
    if let Some(frame_tree) = value.as_object() {
        if let Some(frame) = frame_tree.get("frame") {
            let url = frame.get("url").and_then(|v| v.as_str()).unwrap_or("?").chars().take(60).collect::<String>();
            println!("{}{} id={}", indent, "Frame", url);
        }
        if let Some(children) = frame_tree.get("childFrames").and_then(|v| v.as_array()) {
            for child in children {
                print_frame_tree(child, depth + 1);
            }
        }
    }
    // If it's a direct response from chrome.debugger, the result is in the data field
    if let Some(data) = value.get("frameTree") {
        print_frame_tree(data, depth);
    }
}

/// Collect all frames from the frame tree into a flat list.
fn collect_frames(value: &serde_json::Value) -> Vec<serde_json::Value> {
    let mut frames = Vec::new();
    if let Some(frame_tree) = value.as_object() {
        if let Some(frame) = frame_tree.get("frame") {
            frames.push(frame.clone());
        }
        if let Some(frame_tree_obj) = frame_tree.get("frameTree") {
            frames.extend(collect_frames(frame_tree_obj));
        }
        if let Some(children) = frame_tree.get("childFrames").and_then(|v| v.as_array()) {
            for child in children {
                frames.extend(collect_frames(child));
            }
        }
    }
    frames
}

async fn process_command(
    bs: &BrowserState,
    line: &str,
    media_on: &mut bool,
) -> Result<serde_json::Value> {
    let cmd: serde_json::Value = serde_json::from_str(line)
        .map_err(|e| anyhow::anyhow!("Invalid JSON: {}", e))?;

    let action = cmd.get("action").and_then(|v| v.as_str()).unwrap_or("");

    match action {
        "get_prompt" => Ok(json!({
            "status": "ok",
            "action": "get_prompt",
            "prompt": prompt::AGENT_SYSTEM_PROMPT,
        })),
        "navigate" => handle_navigate(bs, &cmd).await,
        "click" => handle_click(bs, &cmd).await,
        "type" => handle_type(bs, &cmd).await,
        "screenshot" => handle_screenshot(bs).await,
        "tree" => handle_tree(bs).await,
        "meta" => handle_meta(bs).await,
        "get_content" => handle_get_content(bs).await,
        "evaluate" => handle_evaluate(bs, &cmd).await,
        "wait_for" => handle_wait_for(bs, &cmd).await,
        "assert_element" => handle_assert_element(bs, &cmd).await,
        "download_setup" => handle_download_setup(bs, &cmd).await,
        "download" => handle_download(bs, &cmd).await,
        "configure" => handle_configure(bs, &cmd, media_on).await,
        "" => Err(anyhow::anyhow!("Missing 'action' field")),
        _ => Err(anyhow::anyhow!("Unknown action: '{}'", action)),
    }
}

// ─── Command handlers ───────────────────────────────────────────────────

async fn handle_navigate(
    bs: &BrowserState,
    cmd: &serde_json::Value,
) -> Result<serde_json::Value> {
    let url = cmd
        .get("url")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'url' field for navigate"))?;

    bs.navigate(url).await?;

    let tree = bs.extract_tree().await?;
    let meta = bs.get_page_meta().await?;

    Ok(json!({
        "status": "ok",
        "action": "navigate",
        "url": url,
        "title": meta.get("title").and_then(|v| v.as_str()).unwrap_or(""),
        "interactive_count": meta.get("interactiveCount").and_then(|v| v.as_i64()).unwrap_or(0),
        "tree": tree,
    }))
}

async fn handle_click(bs: &BrowserState, cmd: &serde_json::Value) -> Result<serde_json::Value> {
    let target_id = cmd
        .get("target_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'target_id' field for click"))?;

    let (scrolled, tree) = bs.click(target_id).await?;

    Ok(json!({
        "status": "ok",
        "action": "click",
        "target_id": target_id,
        "scrolled": scrolled,
        "tree": tree,
    }))
}

async fn handle_type(bs: &BrowserState, cmd: &serde_json::Value) -> Result<serde_json::Value> {
    let target_id = cmd
        .get("target_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'target_id' field for type"))?;

    let text = cmd
        .get("text")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'text' field for type"))?;

    let tree = bs.type_text(target_id, text).await?;

    Ok(json!({
        "status": "ok",
        "action": "type",
        "target_id": target_id,
        "tree": tree,
    }))
}

async fn handle_screenshot(bs: &BrowserState) -> Result<serde_json::Value> {
    let path = bs.screenshot().await?;

    Ok(json!({
        "status": "ok",
        "action": "screenshot",
        "path": path,
    }))
}

async fn handle_tree(bs: &BrowserState) -> Result<serde_json::Value> {
    let tree = bs.extract_tree().await?;

    Ok(json!({
        "status": "ok",
        "action": "tree",
        "tree": tree,
    }))
}

async fn handle_meta(bs: &BrowserState) -> Result<serde_json::Value> {
    let meta = bs.get_page_meta().await?;

    Ok(json!({
        "status": "ok",
        "action": "meta",
        "meta": meta,
    }))
}

async fn handle_configure(
    bs: &BrowserState,
    cmd: &serde_json::Value,
    media_on: &mut bool,
) -> Result<serde_json::Value> {
    let enabled = cmd
        .get("media_enabled")
        .and_then(|v| v.as_bool())
        .ok_or_else(|| anyhow::anyhow!("Missing 'media_enabled' boolean field for configure"))?;

    let page_handle = {
        let guard = crate::browser::lock_with_timeout(&bs.page, "page").await?;
        guard.clone()
    };
    set_media_blocking_status(&page_handle, enabled).await?;

    *media_on = enabled;

    Ok(json!({
        "status": "ok",
        "action": "configure",
        "media_enabled": enabled,
    }))
}

async fn handle_get_content(bs: &BrowserState) -> Result<serde_json::Value> {
    let content = bs.extract_content().await?;

    Ok(json!({
        "status": "ok",
        "action": "get_content",
        "content": content,
    }))
}

async fn handle_evaluate(
    bs: &BrowserState,
    cmd: &serde_json::Value,
) -> Result<serde_json::Value> {
    let expression = cmd
        .get("expression")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'expression' field for evaluate"))?;

    let result = bs.evaluate(expression).await?;

    Ok(json!({
        "status": "ok",
        "action": "evaluate",
        "result": result,
    }))
}

async fn handle_wait_for(
    bs: &BrowserState,
    cmd: &serde_json::Value,
) -> Result<serde_json::Value> {
    let by = cmd
        .get("by")
        .and_then(|v| v.as_str())
        .unwrap_or("text");

    let value = cmd
        .get("value")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'value' field for wait_for"))?;

    let timeout = cmd
        .get("timeout")
        .and_then(|v| v.as_u64())
        .unwrap_or(10000);

    let result = bs.wait_for_element(by, value, timeout).await?;

    Ok(json!({
        "status": "ok",
        "action": "wait_for",
        "by": by,
        "value": value,
        "result": result,
    }))
}

async fn handle_assert_element(
    bs: &BrowserState,
    cmd: &serde_json::Value,
) -> Result<serde_json::Value> {
    let target_id = cmd
        .get("target_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'target_id' field for assert_element"))?;

    let expected = cmd
        .get("expected")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'expected' field for assert_element"))?;

    let result = bs.assert_element(target_id, expected).await?;

    Ok(json!({
        "status": "ok",
        "action": "assert_element",
        "target_id": target_id,
        "expected": expected,
        "result": result,
    }))
}

async fn handle_download_setup(
    bs: &BrowserState,
    cmd: &serde_json::Value,
) -> Result<serde_json::Value> {
    let path = cmd
        .get("path")
        .and_then(|v| v.as_str())
        .unwrap_or("downloads");

    // Ensure the download directory exists
    let dir = std::path::Path::new(path);
    if !dir.exists() {
        std::fs::create_dir_all(dir)
            .with_context(|| format!("Failed to create download directory: {}", path))?;
    }

    let abs_path = std::fs::canonicalize(dir)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string());

    bs.enable_download(&abs_path).await?;

    Ok(json!({
        "status": "ok",
        "action": "download_setup",
        "path": abs_path,
    }))
}

async fn handle_download(
    bs: &BrowserState,
    cmd: &serde_json::Value,
) -> Result<serde_json::Value> {
    let target_id = cmd
        .get("target_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow::anyhow!("Missing 'target_id' field for download"))?;

    let path = cmd
        .get("path")
        .and_then(|v| v.as_str())
        .unwrap_or("downloads");

    let timeout = cmd
        .get("timeout")
        .and_then(|v| v.as_u64())
        .unwrap_or(30000);

    // Ensure the download directory exists
    let dir = std::path::Path::new(path);
    if !dir.exists() {
        std::fs::create_dir_all(dir)
            .with_context(|| format!("Failed to create download directory: {}", path))?;
    }

    let abs_path = std::fs::canonicalize(dir)
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|_| path.to_string());

    let result = bs.click_and_download(target_id, &abs_path, timeout).await?;

    Ok(json!({
        "status": "ok",
        "action": "download",
        "target_id": target_id,
        "result": result,
    }))
}
