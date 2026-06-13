use anyhow::{Context, Result};
use chromiumoxide::browser::{Browser, BrowserConfig};
use chromiumoxide::cdp::browser_protocol::network::{EnableParams, SetBlockedUrLsParams as SetBlockedURLsParams};
use chromiumoxide::page::Page;
use futures::StreamExt;
use rand::Rng;
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, Mutex};

use crate::injector::{
    EXTRACT_TREE_SCRIPT, HUMAN_TYPE_SCRIPT, INJECT_MARK_SCRIPT, PAGE_META_SCRIPT,
    WAIT_FOR_IDLE_SCRIPT, smart_scroll_script,
};
use crate::utils::{escape_js_string, save_screenshot};

// ═══════════════════════════════════════════════════════════════════════
// Timeouts
//
// EVALUATE_TIMEOUT must exceed HUMAN_TYPE_TIMEOUT so that the JS-side
// hard timeout fires first, returning a clean `false` to Rust rather
// than Rust timing out and receiving a dangling Promise.
// ═══════════════════════════════════════════════════════════════════════

/// CDP evaluate hard ceiling — 35s. Must be > JS typing timeout (30s).
const EVALUATE_TIMEOUT: Duration = Duration::from_secs(35);

/// Navigation hard ceiling — prevents permanent hang on unresponsive URLs.
const NAVIGATE_TIMEOUT: Duration = Duration::from_secs(30);

/// Mutex acquisition timeout — prevents deadlock if CDP handler panics.
const LOCK_TIMEOUT: Duration = Duration::from_secs(5);

/// Network idle wait timeout (Rust-side hard ceiling, independent of JS).
const IDLE_TIMEOUT: Duration = Duration::from_secs(12);

// ═══════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════

/// Acquire a `tokio::sync::Mutex` lock with a timeout.
///
/// R-05 fix: prevents permanent deadlock if the CDP handler task panics
/// while holding a lock. Returns `Err` after [`LOCK_TIMEOUT`].
pub async fn lock_with_timeout<'a, T>(
    mtx: &'a Mutex<T>,
    name: &str,
) -> Result<tokio::sync::MutexGuard<'a, T>> {
    tokio::time::timeout(LOCK_TIMEOUT, mtx.lock())
        .await
        .map_err(|_| anyhow::anyhow!("Mutex '{}' lock timeout ({}s) — possible deadlock", name, LOCK_TIMEOUT.as_secs()))
}

/// Execute a JS evaluate on a page with a hard timeout.
async fn evaluate_with_timeout(
    page: &Page,
    script: &str,
) -> Result<chromiumoxide::js::EvaluationResult> {
    tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(script))
        .await
        .context("CDP evaluate timed out (35s) — page may be unresponsive")?
        .context("CDP evaluate failed")
}

// ═══════════════════════════════════════════════════════════════════════
// Windows Job Object
//
// R-01 fix: assign_pid() is now called with the actual Chrome PID at launch.
// R-04 fix: OpenProcess requests PROCESS_SET_QUOTA | PROCESS_TERMINATE.
// ═══════════════════════════════════════════════════════════════════════

#[cfg(windows)]
mod win_job {
    use std::sync::OnceLock;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    static JOB_HANDLE: OnceLock<HANDLE> = OnceLock::new();

    /// Create a Windows Job Object with `KILL_ON_JOB_CLOSE` semantics.
    /// All child processes assigned to this job are terminated when the
    /// parent process exits (cleanly or via crash/kill).
    pub fn init_job() {
        JOB_HANDLE.get_or_init(|| unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job == 0 {
                eprintln!("[win-job] CreateJobObjectW failed — Chrome may become orphan");
                return INVALID_HANDLE_VALUE;
            }

            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

            let size = std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const _,
                size,
            );

            if ok == 0 {
                eprintln!("[win-job] SetInformationJobObject failed — Chrome may become orphan");
                CloseHandle(job);
                return INVALID_HANDLE_VALUE;
            }

            job
        });
    }

    /// Assign a child process PID to the Job Object.
    /// R-04 fix: request `PROCESS_SET_QUOTA | PROCESS_TERMINATE` as required by MSDN.
    pub fn assign_pid(pid: u32) {
        let &handle = match JOB_HANDLE.get() {
            Some(h) if *h != INVALID_HANDLE_VALUE => h,
            _ => return,
        };
        if pid == 0 {
            return;
        }

        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
        };

        unsafe {
            let proc = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
            if proc == 0 {
                eprintln!("[win-job] OpenProcess({}) failed — cannot assign to job", pid);
                return;
            }
            let ok = AssignProcessToJobObject(handle, proc);
            CloseHandle(proc);
            if ok == 0 {
                eprintln!(
                    "[win-job] AssignProcessToJobObject({}) failed — Chrome may become orphan",
                    pid
                );
            } else {
                eprintln!("[win-job] Chrome PID {} assigned to job object", pid);
            }
        }
    }
}

#[cfg(not(windows))]
mod win_job {
    pub fn init_job() {}
    pub fn assign_pid(_pid: u32) {}
}

// ═══════════════════════════════════════════════════════════════════════
// Resource blocking
// ═══════════════════════════════════════════════════════════════════════

/// URL patterns for resource blocking.
/// R-11 fix: removed overly broad `*pixel*` pattern; replaced with specific ad domains.
const BLOCKED_MEDIA_URLS: &[&str] = &[
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico", "*.bmp", "*.avif",
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
    "*.css",
    "*.mp4", "*.webm", "*.mp3", "*.ogg", "*.wav", "*.flac",
    "*google-analytics.com*", "*googletagmanager.com*", "*facebook.net*",
    "*doubleclick.net*", "*adservice.google.com*", "*pagead2.googlesyndication.com*",
    "*analytics.js*", "*tracking.js*",
    "*pixel.quantserve.com*", "*pixel.rubiconproject.com*",
    "*beacon.krxd.net*", "*beacon.taboola.com*",
];

pub async fn set_media_blocking_status(page: &Page, media_enabled: bool) -> Result<()> {
    if media_enabled {
        page.execute(SetBlockedURLsParams { urls: vec![] })
            .await
            .context("Failed to clear blocked URLs")?;
    } else {
        let blocked: Vec<String> = BLOCKED_MEDIA_URLS.iter().map(|s| s.to_string()).collect();
        page.execute(SetBlockedURLsParams { urls: blocked })
            .await
            .context("Failed to re-apply blocked URLs")?;
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════
// Browser state
//
// Lock ordering convention (MUST be followed by all methods):
//   - No method holds two locks simultaneously across an .await point.
//   - If both page and known_pages are needed, acquire+clone+release
//     the first lock before acquiring the second.
//   - All lock acquisitions use lock_with_timeout() to prevent deadlock.
// ═══════════════════════════════════════════════════════════════════════

pub struct BrowserState {
    pub browser: Browser,
    pub page: Arc<Mutex<Page>>,
    /// R-06 fix: use HashSet for O(1) lookup instead of Vec's O(n).
    known_pages: Arc<Mutex<HashSet<String>>>,
    _shutdown_tx: mpsc::Sender<()>,
}

impl BrowserState {
    pub async fn launch(profile_path: Option<&str>, show: bool) -> Result<Self> {
        win_job::init_job();

        let mut builder = BrowserConfig::builder()
            .window_size(1280, 900)
            .arg("--no-sandbox")
            .arg("--disable-setuid-sandbox")
            .arg("--disable-gpu")
            .arg("--disable-software-rasterizer")
            .arg("--disable-dev-shm-usage")
            .arg("--no-first-run")
            .arg("--disable-default-apps")
            .arg("--disable-popup-blocking")
            .arg("--disable-extensions")
            .arg("--disable-background-networking")
            .arg("--disable-sync")
            .arg("--metrics-recording-only")
            .arg("--safebrowsing-disable-auto-update")
            .arg("--disable-features=IsolateOrigins,site-per-process");

        if show {
            builder = builder.with_head();
        }

        let mut config = builder;

        if let Some(profile) = profile_path {
            config = config.arg(format!("--user-data-dir={}", profile));
        }

        let config = config
            .build()
            .map_err(|e| anyhow::anyhow!("Failed to build browser config: {}", e))?;
        let (mut browser, mut handler) = Browser::launch(config)
            .await
            .context("Failed to launch Chromium. Make sure Chrome or Edge is installed.")?;

        // R-01 fix: assign Chrome PID to the Windows Job Object for orphan prevention.
        if let Some(child) = browser.get_mut_child() {
            let pid = child.inner.id();
            if pid > 0 {
                win_job::assign_pid(pid);
            }
        }

        // CRITICAL FIX: spawn the CDP handler BEFORE any CDP commands.
        // browser.pages() and browser.new_page() send CDP commands through
        // a channel that the handler processes. If the handler isn't running,
        // these calls deadlock.
        let (shutdown_tx, mut shutdown_rx) = mpsc::channel::<()>(1);

        tokio::spawn(async move {
            loop {
                tokio::select! {
                    event = handler.next() => {
                        match event {
                            Some(Ok(_)) => {
                                // Handler processed an event successfully.
                            }
                            Some(Err(e)) => eprintln!("[cdp] {}", e),
                            None => break,
                        }
                    }
                    _ = shutdown_rx.recv() => break,
                }
            }
        });

        // Allow the handler a moment to establish the CDP websocket connection
        tokio::time::sleep(Duration::from_millis(100)).await;

        let initial_pages = browser.pages().await.unwrap_or_default();
        let known_ids: HashSet<String> = initial_pages
            .iter()
            .map(|p| p.target_id().as_ref().to_string())
            .collect();
        let known_pages = Arc::new(Mutex::new(known_ids));

        let current_page = Arc::new(Mutex::new(
            browser
                .new_page("about:blank")
                .await
                .context("Failed to create browser page")?,
        ));

        {
            let p = lock_with_timeout(&current_page, "init_page").await?;
            p.execute(EnableParams::default())
                .await
                .context("Failed to enable network domain")?;
        }

        Ok(Self {
            browser,
            page: current_page,
            known_pages,
            _shutdown_tx: shutdown_tx,
        })
    }

    pub async fn enable_resource_blocking(&self) -> Result<()> {
        let blocked: Vec<String> = BLOCKED_MEDIA_URLS.iter().map(|s| s.to_string()).collect();
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        page.execute(SetBlockedURLsParams { urls: blocked })
            .await
            .context("Failed to set blocked URLs")?;
        Ok(())
    }

    /// Navigate to a URL.
    /// R-03 fix: clone page and release lock before the potentially 30s goto.
    pub async fn navigate(&self, url: &str) -> Result<()> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();

        // Timeout on goto — prevents permanent hang on unresponsive URLs
        let goto_result = tokio::time::timeout(NAVIGATE_TIMEOUT, page.goto(url)).await;

        match goto_result {
            Err(_) => anyhow::bail!("Navigation to {} timed out (30s)", url),
            Ok(inner) => {
                inner.with_context(|| format!("Failed to navigate to {}", url))?;
            }
        }

        // page is dropped here (lock was already released by clone)
        self.wait_for_idle().await;
        self.detect_new_tab().await;
        Ok(())
    }

    pub async fn extract_tree(&self) -> Result<String> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();

        let marked_count = evaluate_with_timeout(&page, INJECT_MARK_SCRIPT)
            .await?
            .value()
            .and_then(|v| v.as_i64())
            .unwrap_or(0);

        if marked_count == 0 {
            return Ok("[No interactive elements found]".to_string());
        }

        let tree = evaluate_with_timeout(&page, EXTRACT_TREE_SCRIPT)
            .await?
            .value()
            .and_then(|v| v.as_str().map(|s| s.to_string()))
            .unwrap_or_else(|| "[Tree extraction returned empty]".to_string());

        Ok(tree)
    }

    pub async fn click(&self, target_id: &str) -> Result<(bool, String)> {
        let safe_id = escape_js_string(target_id);
        let scrolled = self.smart_scroll(&safe_id).await?;

        // R-07 fix: snapshot known tab IDs for atomic comparison
        let known_before = lock_with_timeout(&self.known_pages, "known_pages")
            .await?
            .clone();

        let click_script = format!(
            r#"
            (() => {{
                const id = CSS.escape("{}");
                const el = document.querySelector(`[data-agent-id="${{id}}"]`);
                if (!el) return false;
                el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                el.focus();
                el.click();
                return true;
            }})()
            "#,
            safe_id
        );

        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let clicked = evaluate_with_timeout(&page, &click_script)
            .await?
            .value()
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        if !clicked {
            anyhow::bail!("Element [{}] not found or not clickable", safe_id);
        }

        self.wait_for_idle().await;

        // R-07 fix: detect new tabs via ID set difference, not page count
        self.detect_new_tab_with_known(&known_before).await;

        let tree = self.extract_tree().await?;
        Ok((scrolled, tree))
    }

    pub async fn type_text(&self, target_id: &str, text: &str) -> Result<String> {
        let safe_id = escape_js_string(target_id);
        self.smart_scroll(&safe_id).await?;

        let focus_script = format!(
            r#"
            (() => {{
                const id = CSS.escape("{}");
                const el = document.querySelector(`[data-agent-id="${{id}}"]`);
                if (!el) return false;
                el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
                el.focus();
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{
                    el.value = '';
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                return true;
            }})()
            "#,
            safe_id
        );

        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let focused = evaluate_with_timeout(&page, &focus_script)
            .await?
            .value()
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        if !focused {
            anyhow::bail!("Element [{}] not found for typing", safe_id);
        }

        // Use chars().count() instead of len() — len() returns byte count,
        // which is wrong for multi-byte UTF-8 (CJK, emoji).
        let char_count = text.chars().count();
        let delays: Vec<u64> = (0..char_count)
            .map(|_| {
                let mut rng = rand::thread_rng();
                rng.gen_range(15..=45)
            })
            .collect();

        let char_array = text
            .chars()
            .map(|c| format!("'{}'", c.escape_default()))
            .collect::<Vec<_>>()
            .join(",");

        let delays_json =
            serde_json::to_string(&delays).context("Failed to serialize delays")?;

        let type_script = HUMAN_TYPE_SCRIPT
            .replace("__AGENT_ID__", &safe_id)
            .replace("__AGENT_CHARS__", &format!("[{}]", char_array))
            .replace("__AGENT_DELAYS__", &delays_json);

        // This evaluate may take up to 30s (JS hard timeout).
        // EVALUATE_TIMEOUT (35s) > 30s so JS always resolves first.
        let success = evaluate_with_timeout(&page, &type_script)
            .await?
            .value()
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        if !success {
            anyhow::bail!("Typing in element [{}] failed or timed out", safe_id);
        }

        let total_delay: u64 = delays.iter().sum();
        tokio::time::sleep(Duration::from_millis(total_delay + 300)).await;

        let tree = self.extract_tree().await?;
        Ok(tree)
    }

    pub async fn screenshot(&self) -> Result<String> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let png_data = tokio::time::timeout(
            EVALUATE_TIMEOUT,
            page.screenshot(chromiumoxide::page::ScreenshotParams::builder().build()),
        )
        .await
        .context("Screenshot timed out")?
        .context("Failed to capture screenshot")?;

        let path = save_screenshot(&png_data)?;
        Ok(path)
    }

    async fn smart_scroll(&self, safe_agent_id: &str) -> Result<bool> {
        let scroll_script = smart_scroll_script(safe_agent_id);
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let result = evaluate_with_timeout(&page, &scroll_script).await?;

        let scrolled = result
            .value()
            .and_then(|v| v.get("scrolled").and_then(|s| s.as_bool()))
            .unwrap_or(false);

        if scrolled {
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        Ok(scrolled)
    }

    pub async fn get_page_meta(&self) -> Result<serde_json::Value> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let result = evaluate_with_timeout(&page, PAGE_META_SCRIPT).await?;
        Ok(result
            .value()
            .cloned()
            .unwrap_or(serde_json::Value::Null))
    }

    /// R-16 fix: propagate close errors to caller.
    pub async fn close(&mut self) -> Result<()> {
        self.browser
            .close()
            .await
            .context("Failed to close browser gracefully")?;
        Ok(())
    }

    /// Wait for network idle with a Rust-side hard timeout.
    /// R-08 fix: independent of JS-side timeout; catches cases where
    /// JS idle detection is fooled by WebSocket/EventSource.
    async fn wait_for_idle(&self) {
        let page = match lock_with_timeout(&self.page, "page").await {
            Ok(guard) => guard.clone(),
            Err(e) => {
                eprintln!("[idle] Cannot acquire page lock: {}", e);
                return;
            }
        };

        match tokio::time::timeout(IDLE_TIMEOUT, evaluate_with_timeout(&page, WAIT_FOR_IDLE_SCRIPT))
            .await
        {
            Ok(Ok(r)) => {
                let ok = r.value().and_then(|v| v.as_bool()).unwrap_or(false);
                if !ok {
                    eprintln!("[idle] Network idle wait timed out (JS-side 10s)");
                }
            }
            Ok(Err(e)) => {
                eprintln!("[idle] Network idle check failed: {}", e);
            }
            Err(_) => {
                eprintln!(
                    "[idle] Network idle Rust-side timeout ({}s)",
                    IDLE_TIMEOUT.as_secs()
                );
            }
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }

    /// Detect new tabs by comparing current browser pages against a known set.
    /// Updates the known_pages set and switches focus if a new tab is found.
    async fn detect_new_tab(&self) {
        let current_pages = self.browser.pages().await.unwrap_or_default();
        let current_ids: HashSet<String> = current_pages
            .iter()
            .map(|p| p.target_id().as_ref().to_string())
            .collect();

        let new_tab_id = {
            let known = lock_with_timeout(&self.known_pages, "known_pages").await;
            match known {
                Ok(k) => current_ids.difference(&*k).next().cloned(),
                Err(e) => {
                    eprintln!("[tabs] Cannot acquire known_pages lock: {}", e);
                    return;
                }
            }
        };

        if let Some(new_id) = &new_tab_id {
            // Find the Page handle for the new tab
            if let Some(new_page) = current_pages
                .iter()
                .find(|p| p.target_id().as_ref() == new_id.as_str())
            {
                eprintln!("[tabs] Switching focus to new tab: {}", new_id);
                if let Ok(mut active) = lock_with_timeout(&self.page, "page").await {
                    *active = new_page.clone();
                }
            }
        }

        // Update known set
        if let Ok(mut known) = lock_with_timeout(&self.known_pages, "known_pages").await {
            *known = current_ids;
        }
    }

    /// Detect new tabs given a snapshot of previously known IDs.
    /// R-07 fix: atomic set-difference instead of fragile page count comparison.
    async fn detect_new_tab_with_known(&self, known_before: &HashSet<String>) {
        let current_pages = self.browser.pages().await.unwrap_or_default();
        let current_ids: HashSet<String> = current_pages
            .iter()
            .map(|p| p.target_id().as_ref().to_string())
            .collect();

        let new_tab_id = current_ids.difference(known_before).next().cloned();

        if let Some(new_id) = &new_tab_id {
            if let Some(new_page) = current_pages
                .iter()
                .find(|p| p.target_id().as_ref() == new_id.as_str())
            {
                eprintln!("[tabs] Switching focus to new tab: {}", new_id);
                if let Ok(mut active) = lock_with_timeout(&self.page, "page").await {
                    *active = new_page.clone();
                }
            }
        }

        if let Ok(mut known) = lock_with_timeout(&self.known_pages, "known_pages").await {
            *known = current_ids;
        }
    }
}
