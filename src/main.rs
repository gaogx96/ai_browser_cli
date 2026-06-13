mod browser;
mod injector;
mod utils;

use anyhow::Result;
use clap::{Parser, Subcommand};
use serde_json::json;

use browser::{BrowserState, set_media_blocking_status};
use utils::{spawn_stdin_reader, write_json_stdout};

/// AI Agent Browser CLI - High-performance headless browser control via CDP.
///
/// Designed for AI agents (Python, TypeScript, etc.) to programmatically
/// interact with web pages through stdin/stdout pipe communication.
#[derive(Parser)]
#[command(name = "agent-browser-cli")]
#[command(version = "0.3.0")]
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

        /// Chrome profile path for session reuse (e.g., C:\Users\You\AppData\...)
        #[arg(short, long)]
        profile: Option<String>,

        /// Show browser window (disable headless mode)
        #[arg(long, default_value = "false")]
        show: bool,
    },

    /// Persistent pipe mode: listen on stdin for JSON commands, respond on stdout.
    ///
    /// Commands format (one JSON per line):
    ///   {"action": "navigate", "url": "https://..."}
    ///   {"action": "click", "target_id": "e5"}
    ///   {"action": "type", "target_id": "e3", "text": "hello"}
    ///   {"action": "screenshot"}
    ///   {"action": "configure", "media_enabled": true}
    ///
    /// Exits cleanly when stdin pipe is closed (parent process exits).
    Listen {
        /// Chrome profile path for session reuse
        #[arg(short, long)]
        profile: Option<String>,

        /// Enable aggressive resource blocking (images, CSS, fonts, ads)
        #[arg(short, long, default_value = "true")]
        block_resources: bool,

        /// Start with media loading enabled (skip initial blocking).
        /// Default false = aggressive blocking from the start.
        #[arg(long, default_value = "false")]
        media_enabled: bool,

        /// Show browser window (disable headless mode)
        #[arg(long, default_value = "false")]
        show: bool,
    },
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    let result = match cli.command {
        Commands::View { url, profile, show } => cmd_view(&url, profile.as_deref(), show).await,
        Commands::Listen {
            profile,
            block_resources,
            media_enabled,
            show,
        } => cmd_listen(profile.as_deref(), block_resources, media_enabled, show).await,
    };

    // R-02 fix: NO process::exit() here. Normal return lets Drop run,
    // which fires _shutdown_tx, cleanly stops the CDP handler, and
    // the browser's Drop kills Chrome via kill_on_drop(true).
    if let Err(e) = &result {
        let error_json = json!({
            "status": "error",
            "error": format!("{:#}", e)
        });
        // Best-effort error output; if stdout is broken, just exit.
        let _ = write_json_stdout(&error_json).await;
        std::process::exit(1);
    }
}

/// One-shot view: load URL, extract tree, print to stdout, exit.
async fn cmd_view(url: &str, profile: Option<&str>, show: bool) -> Result<()> {
    let mut bs = BrowserState::launch(profile, show).await?;

    bs.enable_resource_blocking().await?;
    bs.navigate(url).await?;

    let tree = bs.extract_tree().await?;
    let meta = bs.get_page_meta().await?;

    let output = json!({
        "status": "ok",
        "url": url,
        "title": meta.get("title").and_then(|v| v.as_str()).unwrap_or(""),
        "interactive_count": meta.get("interactiveCount").and_then(|v| v.as_i64()).unwrap_or(0),
        "tree": tree,
    });

    write_json_stdout(&output).await?;
    // R-16 fix: propagate close errors
    bs.close().await?;

    Ok(())
}

/// Persistent listen mode: stdin/stdout pipe communication.
///
/// Architecture:
/// - Stdin is read via `spawn_blocking` to avoid starving the tokio runtime
/// - Stdout is written via `spawn_blocking` to avoid pipe-buffer blocking
/// - The background CDP event handler has a shutdown channel
/// - R-02 fix: NO process::exit(). All paths return Result, letting Drop
///   clean up the browser and fire the shutdown channel.
async fn cmd_listen(
    profile: Option<&str>,
    block_resources: bool,
    media_enabled: bool,
    show: bool,
) -> Result<()> {
    let mut bs = BrowserState::launch(profile, show).await?;

    // Apply initial blocking policy
    if block_resources && !media_enabled {
        bs.enable_resource_blocking().await?;
    }

    let mut media_on = media_enabled;

    // Send ready signal
    let ready = json!({
        "status": "ready",
        "media_enabled": media_on,
        "message": "agent-browser-cli is listening"
    });
    write_json_stdout(&ready).await?;

    // Spawn async stdin reader (non-blocking)
    let mut stdin_rx = spawn_stdin_reader();

    // Main command loop — all I/O is async, no thread starvation
    loop {
        let line = match stdin_rx.recv().await {
            Some(line) => line,
            None => {
                // R-02 fix: return Ok instead of process::exit(0).
                // bs is dropped here → _shutdown_tx fires → handler exits → browser killed.
                eprintln!("[listen] Stdin closed. Shutting down...");
                bs.close().await.ok(); // best-effort close
                return Ok(());
            }
        };

        if line.is_empty() {
            continue;
        }

        let cmd: serde_json::Value = match serde_json::from_str(&line) {
            Ok(v) => v,
            Err(e) => {
                let err_resp = json!({
                    "status": "error",
                    "error": format!("Invalid JSON: {}", e)
                });
                write_json_stdout(&err_resp).await?;
                continue;
            }
        };

        let action = cmd.get("action").and_then(|v| v.as_str()).unwrap_or("");

        let response = match action {
            "navigate" => handle_navigate(&bs, &cmd).await,
            "click" => handle_click(&bs, &cmd).await,
            "type" => handle_type(&bs, &cmd).await,
            "screenshot" => handle_screenshot(&bs).await,
            "tree" => handle_tree(&bs).await,
            "meta" => handle_meta(&bs).await,
            "configure" => handle_configure(&bs, &cmd, &mut media_on).await,
            "" => Err(anyhow::anyhow!("Missing 'action' field")),
            _ => Err(anyhow::anyhow!("Unknown action: '{}'", action)),
        };

        let resp_json = match response {
            Ok(data) => data,
            Err(e) => json!({
                "status": "error",
                "error": format!("{:#}", e)
            }),
        };

        if write_json_stdout(&resp_json).await.is_err() {
            // R-02 fix: return instead of process::exit(1)
            eprintln!("[listen] Stdout write failed. Shutting down...");
            bs.close().await.ok();
            return Err(anyhow::anyhow!("stdout write failed"));
        }
    }
}

// ─── Command handlers ───────────────────────────────────────────────────

async fn handle_navigate(bs: &BrowserState, cmd: &serde_json::Value) -> Result<serde_json::Value> {
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
