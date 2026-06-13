use anyhow::{Context, Result};
use chrono::Utc;
use std::fs;

// M-03 fix: spawn_stdin_reader removed.
// Stdin is now read via async tokio::io::BufReader in main.rs.
// No more spawn_blocking threads blocking on stdin.lock().read_line().

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
/// L-01 fix: falls back to temp directory if exe_dir is not writable.
pub fn save_screenshot(png_data: &[u8]) -> Result<String> {
    // Try exe directory first, fall back to temp dir if not writable
    let dump_dir = match std::env::current_exe() {
        Ok(exe_path) => {
            let dir = exe_path.parent().unwrap_or(std::path::Path::new(".")).join("debug_dumps");
            // Test writability by attempting to create the directory
            if ensure_dir_writable(&dir).is_ok() {
                dir
            } else {
                // L-01 fix: fallback to temp directory
                let tmp = std::env::temp_dir().join("agent-browser-cli").join("debug_dumps");
                ensure_dir_writable(&tmp)
                    .with_context(|| format!("Cannot write to fallback directory: {}", tmp.display()))?;
                tmp
            }
        }
        Err(_) => {
            let tmp = std::env::temp_dir().join("agent-browser-cli").join("debug_dumps");
            ensure_dir_writable(&tmp)
                .with_context(|| format!("Cannot write to fallback directory: {}", tmp.display()))?;
            tmp
        }
    };

    let timestamp = Utc::now().timestamp_millis();
    // Hermes #16 fix: u32 nonce (4 billion possibilities) instead of u16 (65536)
    let nonce: u32 = rand::random();
    let filename = format!("agent_capture_{:08x}_{}.png", nonce, timestamp);
    let file_path = dump_dir.join(&filename);

    fs::write(&file_path, png_data)
        .with_context(|| format!("Failed to write screenshot to {}", file_path.display()))?;

    Ok(file_path.to_string_lossy().to_string())
}

/// Ensure a directory exists and is writable. Creates it if it doesn't exist.
fn ensure_dir_writable(dir: &std::path::Path) -> Result<()> {
    if !dir.exists() {
        fs::create_dir_all(dir)
            .with_context(|| format!("Failed to create directory: {}", dir.display()))?;
    }
    // Verify writability by checking directory metadata on Windows
    #[cfg(windows)]
    {
        let metadata = fs::metadata(dir)
            .with_context(|| format!("Cannot read directory metadata: {}", dir.display()))?;
        if metadata.permissions().readonly() {
            anyhow::bail!("Directory is read-only: {}", dir.display());
        }
    }
    Ok(())
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

    #[test]
    fn test_escape_js_string_unicode_separators() {
        assert_eq!(escape_js_string("a\u{2028}b"), "a\\u2028b");
        assert_eq!(escape_js_string("a\u{2029}b"), "a\\u2029b");
    }

    #[test]
    fn test_escape_js_string_empty() {
        assert_eq!(escape_js_string(""), "");
    }
}
