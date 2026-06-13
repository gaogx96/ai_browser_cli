use anyhow::{Context, Result};
use chrono::Utc;
use std::fs;
use tokio::sync::mpsc;

// ─── Constants ──────────────────────────────────────────────────────────

/// Maximum single line length from stdin (1 MB). Prevents OOM from malformed input.
const MAX_STDIN_LINE_BYTES: usize = 1024 * 1024;

// ─── Async-safe stdin reader ────────────────────────────────────────────

/// Spawn a blocking stdin reader that feeds lines through an async mpsc channel.
///
/// Returns a receiver that yields `Some(line)` for each stdin line,
/// and `None` when stdin is closed (EOF / pipe broken).
///
/// Lines exceeding [`MAX_STDIN_LINE_BYTES`] are discarded with an error log.
pub fn spawn_stdin_reader() -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel::<String>(64); // bounded backpressure

    tokio::task::spawn_blocking(move || {
        use std::io::{self, BufRead};
        let stdin = io::stdin();

        loop {
            let mut line = String::new();
            match stdin.lock().read_line(&mut line) {
                Ok(0) => {
                    // EOF — parent process closed the pipe
                    break;
                }
                Ok(_) => {
                    // R-10 fix: reject oversized lines
                    if line.len() > MAX_STDIN_LINE_BYTES {
                        eprintln!(
                            "[stdin] Line too large ({} bytes, max {}), discarding",
                            line.len(),
                            MAX_STDIN_LINE_BYTES
                        );
                        continue;
                    }
                    let trimmed = line.trim().to_string();
                    if tx.blocking_send(trimmed).is_err() {
                        // Receiver dropped — main loop exited
                        break;
                    }
                }
                Err(e) => {
                    eprintln!("[stdin] Read error (pipe broken): {}", e);
                    break;
                }
            }
        }
        // tx drops here, receiver will get None
    });

    rx
}

// ─── Async-safe stdout writer ───────────────────────────────────────────

/// Write a JSON result to stdout using `spawn_blocking` to avoid async starvation.
///
/// `println!()` can block if the stdout pipe buffer is full (e.g., the parent
/// process is slow to read). We move the actual write to a blocking thread.
pub async fn write_json_stdout(value: &serde_json::Value) -> Result<()> {
    let json_str = serde_json::to_string(value).context("Failed to serialize JSON")?;

    tokio::task::spawn_blocking(move || {
        println!("{}", json_str);
    })
    .await
    .context("stdout write task failed")?;

    Ok(())
}

// ─── Screenshot utilities ───────────────────────────────────────────────

/// Save a PNG screenshot to a `debug_dumps` folder next to the executable.
///
/// Uses millisecond timestamp + 4-digit random suffix to prevent collisions.
/// R-12 fix: uses exe directory instead of CWD for deterministic paths.
pub fn save_screenshot(png_data: &[u8]) -> Result<String> {
    let exe_dir = std::env::current_exe()
        .context("Failed to get executable path")?
        .parent()
        .context("Failed to get executable directory")?
        .to_path_buf();
    let dump_dir = exe_dir.join("debug_dumps");

    if !dump_dir.exists() {
        fs::create_dir_all(&dump_dir)
            .with_context(|| format!("Failed to create directory: {}", dump_dir.display()))?;
    }

    let timestamp = Utc::now().timestamp_millis();
    let nonce: u16 = rand::random();
    let filename = format!("agent_capture_{:04x}_{}.png", nonce & 0xFFFF, timestamp);
    let file_path = dump_dir.join(&filename);

    fs::write(&file_path, png_data)
        .with_context(|| format!("Failed to write screenshot to {}", file_path.display()))?;

    Ok(file_path.to_string_lossy().to_string())
}

// ─── JS string escaping ─────────────────────────────────────────────────

/// Escape a string for safe embedding inside a JavaScript string literal.
///
/// Handles: backslash, single quote, double quote, backtick, newlines,
/// carriage returns, tabs, null bytes, and Unicode line/paragraph separators.
///
/// Use this for any external input that will be interpolated into JS code.
pub fn escape_js_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 16);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            '"' => out.push_str("\\\""),
            '`' => out.push_str("\\`"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\0' => out.push_str("\\0"),
            // Unicode line/paragraph separators
            '\u{2028}' => out.push_str("\\u2028"),
            '\u{2029}' => out.push_str("\\u2029"),
            _ => out.push(c),
        }
    }
    out
}

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_escape_js_string() {
        assert_eq!(escape_js_string("hello"), "hello");
        assert_eq!(escape_js_string(r#"it's "ok""#), r#"it\'s \"ok\""#);
        assert_eq!(escape_js_string("line1\nline2"), "line1\\nline2");
        assert_eq!(escape_js_string("a\\b"), "a\\\\b");
        assert_eq!(escape_js_string("a`b"), "a\\`b");
        assert_eq!(escape_js_string("null\0byte"), "null\\0byte");
    }
}
