mod browser;
mod injector;
mod prompt;
mod utils;

use std::time::Duration;

use anyhow::Result;
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
    },

    /// Print the AI Agent system prompt to stdout and exit.
    /// Use this to feed the prompt into your LLM pipeline.
    Prompt,
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
        Commands::View { url, profile, connect, show } => {
            cmd_view(&url, profile.as_deref(), connect.as_deref(), show).await
        }
        Commands::Listen {
            profile,
            connect,
            resources,
            show,
        } => {
            cmd_listen(
                profile.as_deref(),
                connect.as_deref(),
                &resources,
                show,
            )
            .await
        }
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
