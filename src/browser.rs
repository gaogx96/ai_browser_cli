use anyhow::{Context, Result};
use chromiumoxide::browser::{Browser, BrowserConfig};
use chromiumoxide::cdp::browser_protocol::browser::{
    SetDownloadBehaviorBehavior, SetDownloadBehaviorParams,
};
use chromiumoxide::cdp::browser_protocol::network::{
    EnableParams, EventLoadingFailed, EventLoadingFinished, EventRequestWillBeSent,
    EventResponseReceived, SetBlockedUrLsParams as SetBlockedURLsParams,
};
use chromiumoxide::cdp::browser_protocol::page::{
    CreateIsolatedWorldParams, FrameId, FrameTree, GetFrameTreeParams,
};
use chromiumoxide::cdp::browser_protocol::target::SetAutoAttachParams;
use chromiumoxide::cdp::js_protocol::runtime::EvaluateParams;
use chromiumoxide::page::Page;
use chromiumoxide::cdp::js_protocol::runtime::ExecutionContextId;
use futures::StreamExt;
use rand::Rng;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, oneshot, Mutex, Notify};

use crate::injector::{
    ANTI_FINGERPRINT_SCRIPT, ASSERT_ELEMENT_SCRIPT, EXTRACT_CONTENT_SCRIPT,
    EXTRACT_TREE_SCRIPT, HUMAN_TYPE_SCRIPT, PAGE_META_SCRIPT, WAIT_FOR_ELEMENT_SCRIPT,
    smart_scroll_script,
};
use crate::utils::{escape_js_string, save_screenshot};

// ═══════════════════════════════════════════════════════════════════════
// Timeouts
// ═══════════════════════════════════════════════════════════════════════

const EVALUATE_TIMEOUT: Duration = Duration::from_secs(35);
const NAVIGATE_TIMEOUT: Duration = Duration::from_secs(30);
const LOCK_TIMEOUT: Duration = Duration::from_secs(5);
const IDLE_TIMEOUT: Duration = Duration::from_secs(12);
const HANDLER_READY_TIMEOUT: Duration = Duration::from_secs(5);
const CDP_OP_TIMEOUT: Duration = Duration::from_secs(30);
const IDLE_DEBOUNCE: Duration = Duration::from_millis(200);

// ═══════════════════════════════════════════════════════════════════════
// Validation
// ═══════════════════════════════════════════════════════════════════════

const MAX_TARGET_ID_LEN: usize = 32;
const MAX_TEXT_LEN: usize = 10_000;

fn validate_target_id(id: &str) -> Result<()> {
    if id.len() > MAX_TARGET_ID_LEN {
        anyhow::bail!("target_id too long ({} chars, max {})", id.len(), MAX_TARGET_ID_LEN);
    }
    if id.is_empty() {
        anyhow::bail!("target_id must not be empty");
    }
    // Accept both legacy format "e5" and frame-prefixed format "f0-e5"
    let bytes = id.as_bytes();
    if bytes[0] == b'e' && bytes[1..].iter().all(|b| b.is_ascii_digit()) {
        return Ok(()); // legacy format
    }
    // Frame-prefixed format: f{idx}-e{local_id}
    if bytes[0] == b'f' {
        let dash_pos = id.find('-');
        if let Some(pos) = dash_pos {
            let idx_part = &id[1..pos];
            let local_part = &id[pos+1..];
            if idx_part.bytes().all(|b| b.is_ascii_digit())
                && local_part.len() > 1
                && local_part.as_bytes()[0] == b'e'
                && local_part[1..].bytes().all(|b| b.is_ascii_digit())
            {
                return Ok(());
            }
        }
    }
    anyhow::bail!(
        "target_id has invalid format: '{}'. Expected 'e5' or 'f0-e5'",
        id
    );
}

fn validate_text(text: &str) -> Result<()> {
    if text.len() > MAX_TEXT_LEN {
        anyhow::bail!("text too long ({} bytes, max {})", text.len(), MAX_TEXT_LEN);
    }
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════════════════

pub async fn lock_with_timeout<'a, T>(
    mtx: &'a Mutex<T>,
    name: &str,
) -> Result<tokio::sync::MutexGuard<'a, T>> {
    tokio::time::timeout(LOCK_TIMEOUT, mtx.lock())
        .await
        .map_err(|_| {
            anyhow::anyhow!(
                "Mutex '{}' lock timeout ({}s) — possible deadlock",
                name,
                LOCK_TIMEOUT.as_secs()
            )
        })
}

async fn evaluate_with_timeout(
    page: &Page,
    script: &str,
) -> Result<chromiumoxide::js::EvaluationResult> {
    tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(script))
        .await
        .context("CDP evaluate timed out (35s) — page may be unresponsive")?
        .context("CDP evaluate failed")
}

async fn cdp_op_with_timeout<F, T, E>(future: F, op_name: &str) -> Result<T>
where
    F: std::future::Future<Output = std::result::Result<T, E>>,
    E: Into<anyhow::Error>,
{
    tokio::time::timeout(CDP_OP_TIMEOUT, future)
        .await
        .map_err(|_| {
            anyhow::anyhow!(
                "CDP operation '{}' timed out ({}s) — handler may have crashed",
                op_name,
                CDP_OP_TIMEOUT.as_secs()
            )
        })?
        .map_err(Into::into)
}

// ═══════════════════════════════════════════════════════════════════════
// Windows Job Object — M-01 fix: RAII wrapper with LazyLock
// ═══════════════════════════════════════════════════════════════════════

#[cfg(windows)]
mod win_job {
    use std::sync::{LazyLock, Mutex};
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    struct JobHandle(HANDLE);

    impl Drop for JobHandle {
        fn drop(&mut self) {
            let _ = self.0;
        }
    }

    unsafe impl Send for JobHandle {}
    unsafe impl Sync for JobHandle {}

    static JOB: LazyLock<Mutex<Option<JobHandle>>> = LazyLock::new(|| {
        let handle = unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job == 0 {
                eprintln!("[win-job] CRITICAL: CreateJobObjectW failed — Chrome may become orphan");
                return Mutex::new(None);
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
                eprintln!("[win-job] CRITICAL: SetInformationJobObject failed");
                CloseHandle(job);
                return Mutex::new(None);
            }
            eprintln!("[win-job] Job Object created successfully");
            job
        };
        Mutex::new(Some(JobHandle(handle)))
    });

    pub fn init_job() -> bool {
        JOB.lock().map(|g| g.is_some()).unwrap_or(false)
    }

    pub fn assign_pid(pid: u32) -> bool {
        let guard = match JOB.lock() {
            Ok(g) => g,
            Err(_) => return false,
        };
        let handle = match guard.as_ref() {
            Some(jh) => jh.0,
            None => return false,
        };
        if pid == 0 {
            return false;
        }
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
        };
        unsafe {
            let proc = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
            if proc == 0 {
                return false;
            }
            let ok = AssignProcessToJobObject(handle, proc);
            CloseHandle(proc);
            if ok == 0 {
                eprintln!("[win-job] AssignProcessToJobObject({}) failed", pid);
                false
            } else {
                eprintln!("[win-job] Chrome PID {} assigned to job object", pid);
                true
            }
        }
    }
}

#[cfg(not(windows))]
mod win_job {
    pub fn init_job() -> bool { true }
    pub fn assign_pid(_pid: u32) -> bool { true }
}

// ═══════════════════════════════════════════════════════════════════════
// Resource blocking
// ═══════════════════════════════════════════════════════════════════════

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

fn blocked_urls_cached() -> Vec<String> {
    use std::sync::OnceLock;
    static CACHED: OnceLock<Vec<String>> = OnceLock::new();
    CACHED
        .get_or_init(|| BLOCKED_MEDIA_URLS.iter().map(|s| s.to_string()).collect())
        .clone()
}

/// Ad/tracking-only blocklist for "smart" mode.
/// Blocks ads and analytics but allows images, CSS, fonts.
const AD_ONLY_URLS: &[&str] = &[
    "*google-analytics.com*", "*googletagmanager.com*", "*facebook.net*",
    "*doubleclick.net*", "*adservice.google.com*", "*pagead2.googlesyndication.com*",
    "*analytics.js*", "*tracking.js*",
    "*pixel.quantserve.com*", "*pixel.rubiconproject.com*",
    "*beacon.krxd.net*", "*beacon.taboola.com*",
    "*hm.baidu.com*", "*cpro.baidu.com*", "*baidustatic.com*",
    "*beacon.qq.com*", "*cnzz.com*", "*umeng.com*",
    "*sentry.io*", "*newrelic.com*", "*hotjar.com*", "*clarity.ms*",
];

fn ad_only_urls_cached() -> Vec<String> {
    use std::sync::OnceLock;
    static CACHED: OnceLock<Vec<String>> = OnceLock::new();
    CACHED
        .get_or_init(|| AD_ONLY_URLS.iter().map(|s| s.to_string()).collect())
        .clone()
}

pub async fn set_media_blocking_status(page: &Page, media_enabled: bool) -> Result<()> {
    if media_enabled {
        page.execute(SetBlockedURLsParams { urls: vec![] })
            .await
            .context("Failed to clear blocked URLs")?;
    } else {
        page.execute(SetBlockedURLsParams { urls: blocked_urls_cached() })
            .await
            .context("Failed to re-apply blocked URLs")?;
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════
// Network Idle Tracker — Multi-Dimensional Hybrid Filter
//
// Three-layer interception matrix + resource-type gating ensures that
// analytics/telemetry/beacon requests never prevent idle detection.
// ═══════════════════════════════════════════════════════════════════════

use chromiumoxide::cdp::browser_protocol::network::ResourceType;

// ── Layer 1: Top-level analytics domain blacklist (suffix match) ──────
// Covers global + Chinese domestic tracking infrastructure.

const DOMAIN_BLACKLIST: &[&str] = &[
    // Google
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "adservice.google.com", "pagead2.googlesyndication.com",
    "googlesyndication.com", "google.com/pagead", "googleadservices.com",
    // Facebook / Meta
    "facebook.com/tr", "facebook.net", "connect.facebook.net",
    // Baidu
    "hm.baidu.com", "cpro.baidu.com", "pos.baidu.com", "drmcmm.baidu.com",
    "eclick.baidu.com", "wangmeng.baidu.com", "baidustatic.com",
    "baidu.com/ecom", "baidu.com/hm", "baidu.com/baidu.php",
    "nsclick.baidu.com", "entry.baidu.com", "baidu.com/zt/",
    // Alibaba
    "tanx.com", "mmstat.com", "alicdn.com/s.gif", "taobao.com/go/",
    "alimama.com", "atpanel.com", "yimg.com",
    // Tencent
    "beacon.qq.com", "pingtas.qq.com", "report.qqbrowser.com",
    "tdw.qq.com", "btrace.qq.com", "pingma.qq.com",
    "beacon.cdn.qq.com", "qbox.me", "gtimg.cn",
    // GrowingIO
    "growingio.com", "gio.growingio.com",
    // CNZZ / Umeng
    "cnzz.com", "umeng.com", "alog.umeng.com", "ar.umeng.com",
    // Segment / Mixpanel / Amplitude
    "segment.io", "segment.com", "api.segment.io",
    "mixpanel.com", "api.mixpanel.com",
    "amplitude.com", "api.amplitude.com",
    // Hotjar / Clarity / Bing
    "hotjar.com", "clarity.ms", "bat.bing.com",
    // Sentry / NewRelic
    "sentry.io", "ingest.sentry.io",
    "newrelic.com", "nr-data.net", "bam.nr-data.net",
    // Yandex
    "mc.yandex.ru", "yandex.ru/clck",
    // 1px tracking domains
    "scorecardresearch.com", "doubleverify.com", "adsrvr.org",
    "adsafeprotected.com", "moatads.com", "quantserve.com",
    "rubiconproject.com", "taboola.com", "outbrain.com",
    "criteo.com", "casalemedia.com", "openx.net",
    "pubmatic.com", "sharethrough.com", "spotxchange.com",
];

// ── Layer 2: Path keyword patterns (behavioral match) ────────────────
// Matches URL path segments that indicate tracking actions.

const PATH_KEYWORDS: &[&str] = &[
    "/track", "/tracking", "/click", "/collect", "/collect?",
    "/log", "/log?", "/log/", "/report", "/beacon",
    "/analytics", "/telemetry", "/event_tracking", "/event",
    "/pv", "/pv?", "/pageview", "/impression",
    "/pixel", "/1x1", "/spacer.gif", "/blank.gif",
    "/stat", "/stat?", "/stats", "/stats/",
    "/monitor", "/rum", "/ping", "/ping?",
    "/omniture", "/gtm", "/ga?", "/utm.gif",
    "/baidu.php", "/hm.gif", "/eclick.php",
    "/__utm", "/utm.gif", "/__gtm",
    "/ad/", "/ads/", "/adv/", "/banner/",
];

// ── Layer 3: Beacon file extensions ─────────────────────────────────
// 1x1 transparent GIF/PNG used as tracking beacons.

const BEACON_EXTENSIONS: &[&str] = &[
    ".gif?", ".gif#",  // e.g., hm.baidu.com/hm.gif?...
    ".png?", ".png#",  // beacon png with query params
    ".1x1", ".0x0",
    "/spacer.gif", "/blank.gif", "/clear.gif", "/pixel.gif",
    "/1x1.gif", "/1x1.png", "/b.gif", "/s.gif",
];

/// Multi-dimensional hybrid filter: returns true if the request should
/// be EXCLUDED from idle counting (i.e., it's analytics/beacon/tracking).
fn should_skip_for_idle(url: &str, resource_type: &Option<ResourceType>) -> bool {
    // ── Resource type gate (fastest check) ──
    // Image, Ping, Other are almost always non-essential for idle detection.
    match resource_type {
        Some(ResourceType::Image) => return true,    // beacon GIF/PNG
        Some(ResourceType::Ping) => return true,     // navigator.sendBeacon
        Some(ResourceType::Stylesheet) => return true,
        Some(ResourceType::Font) => return true,
        Some(ResourceType::Media) => return true,
        Some(ResourceType::Prefetch) => return true,
        Some(ResourceType::Manifest) => return true,
        Some(ResourceType::SignedExchange) => return true,
        Some(ResourceType::TextTrack) => return true,
        _ => {}
    }

    let lower = url.to_ascii_lowercase();

    // ── Layer 1: Domain blacklist (suffix match) ──
    // Strip protocol prefix for matching
    let domain_part = lower
        .split("://")
        .last()
        .unwrap_or(&lower);

    if DOMAIN_BLACKLIST.iter().any(|d| domain_part.starts_with(d) || domain_part.contains(d)) {
        return true;
    }

    // ── Layer 2: Path keyword match ──
    if PATH_KEYWORDS.iter().any(|kw| lower.contains(kw)) {
        return true;
    }

    // ── Layer 3: Beacon file extension match ──
    if BEACON_EXTENSIONS.iter().any(|ext| lower.contains(ext)) {
        return true;
    }

    false
}

// R-06 fix: LoaderId-isolated network idle tracker.
//
// Instead of a single global AtomicUsize, we track active request counts
// per LoaderId. Each CDP navigation produces a unique LoaderId, and each
// request's lifecycle (requestWillBeSent → loadingFinished/loadingFailed)
// is scoped to its LoaderId. This prevents stale events from page A
// polluting the idle detection of page B.
//
// Architecture:
//   - request_counts: HashMap<LoaderId, usize> — active requests per loader
//   - request_to_loader: HashMap<RequestId, LoaderId> — maps request to its loader
//   - active_loader: the current LoaderId we're waiting on
//   - notify: wakes wait_for_idle when the active loader's count hits 0


struct NetworkIdleTracker {
    /// Per-loader active request counts.
    request_counts: Mutex<std::collections::HashMap<String, usize>>,
    /// Maps request_id → loader_id for decrement routing.
    request_to_loader: Mutex<std::collections::HashMap<String, String>>,
    /// The LoaderId of the current navigation we're waiting on.
    active_loader: Mutex<String>,
    /// Notified when the active loader's count reaches 0.
    notify: Notify,
}

impl NetworkIdleTracker {
    fn new() -> Self {
        Self {
            request_counts: Mutex::new(std::collections::HashMap::new()),
            request_to_loader: Mutex::new(std::collections::HashMap::new()),
            active_loader: Mutex::new(String::new()),
            notify: Notify::new(),
        }
    }

    /// Called on requestWillBeSent. Increments the count for this request's loader.
    async fn on_request_start(
        &self,
        loader_id: &str,
        request_id: &str,
        url: &str,
        resource_type: &Option<ResourceType>,
    ) {
        if should_skip_for_idle(url, resource_type) {
            return;
        }

        // Record request → loader mapping
        {
            let mut mapping = self.request_to_loader.lock().await;
            mapping.insert(request_id.to_string(), loader_id.to_string());
        }

        // Increment loader's count
        {
            let mut counts = self.request_counts.lock().await;
            let entry = counts.entry(loader_id.to_string()).or_insert(0);
            *entry += 1;
        }
    }

    /// Called on loadingFinished/loadingFailed. Decrements the count for
    /// the loader that owns this request.
    async fn on_request_done(&self, request_id: &str) {
        // Look up which loader this request belongs to
        let loader_id = {
            let mut mapping = self.request_to_loader.lock().await;
            mapping.remove(request_id)
        };

        let loader_id = match loader_id {
            Some(id) => id,
            None => return, // Unknown request, ignore
        };

        // Decrement the loader's count
        let became_zero = {
            let mut counts = self.request_counts.lock().await;
            if let Some(count) = counts.get_mut(&loader_id) {
                *count = count.saturating_sub(1);
                let is_zero = *count == 0;
                if is_zero {
                    counts.remove(&loader_id);
                }
                is_zero
            } else {
                false
            }
        };

        // If this was the active loader and it just hit 0, notify
        if became_zero {
            let active = self.active_loader.lock().await;
            if loader_id == *active {
                self.notify.notify_one();
            }
        }
    }

    /// Check if the active loader has no pending requests.
    async fn is_idle(&self) -> bool {
        let active = self.active_loader.lock().await;
        if active.is_empty() {
            return true;
        }
        let counts = self.request_counts.lock().await;
        counts.get(&*active).copied().unwrap_or(0) == 0
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Browser state
// ═══════════════════════════════════════════════════════════════════════

pub struct BrowserState {
    pub browser: Browser,
    pub page: Arc<Mutex<Page>>,
    known_pages: Arc<Mutex<HashSet<String>>>,
    /// Frame index → FrameId mapping (snapshot from last extract_tree).
    /// Used by click/type to find the correct frame for a f{idx}-e{local} ID.
    frame_map: Arc<Mutex<HashMap<usize, FrameId>>>,
    idle_tracker: Arc<NetworkIdleTracker>,
    _shutdown_tx: mpsc::Sender<()>,
    /// R-01 fix: abort handles for network listener tasks.
    /// When switching tabs, old listeners are aborted before spawning new ones.
    listener_handles: Mutex<Vec<tokio::task::AbortHandle>>,
    /// True if we connected to an existing Chrome (via --connect).
    /// In this mode, close() must NOT send Browser.close — that would
    /// kill the user's Chrome. We just disconnect instead.
    connected: bool,
}

impl BrowserState {
    /// Spawn the CDP handler task and return the shutdown channel.
    /// Shared between `launch` and `connect` to avoid code duplication.
    fn spawn_handler(
        mut handler: chromiumoxide::handler::Handler,
    ) -> (mpsc::Sender<()>, oneshot::Receiver<()>) {
        let (ready_tx, ready_rx) = oneshot::channel::<()>();
        let (shutdown_tx, mut shutdown_rx) = mpsc::channel::<()>(1);

        tokio::spawn(async move {
            let mut ready_tx = Some(ready_tx);
            loop {
                tokio::select! {
                    event = handler.next() => {
                        match event {
                            Some(Ok(_)) => {
                                if let Some(tx) = ready_tx.take() {
                                    let _ = tx.send(());
                                }
                            }
                            // Fix 2: suppress CDP untagged enum warnings.
                            // These are non-fatal parse errors from newer Chromium
                            // versions sending event fields that chromiumoxide doesn't
                            // recognize. We silently discard them.
                            Some(Err(_e)) => {
                                if let Some(tx) = ready_tx.take() {
                                    let _ = tx.send(());
                                }
                                // Intentionally NOT printing eprintln here.
                                // The "data did not match any variant of untagged enum"
                                // warnings are harmless and spam stderr.
                            }
                            None => break,
                        }
                    }
                    _ = shutdown_rx.recv() => break,
                }
            }
        });

        (shutdown_tx, ready_rx)
    }

    /// Wait for the CDP handler to be ready, with timeout.
    /// In connect mode, uses 500ms because the existing Chrome already has
    /// established sessions — no need to wait for full tab synchronization.
    /// Our CDP network idle detection + 12s hard timeout handle the rest.
    async fn wait_for_handler(ready_rx: oneshot::Receiver<()>, connect_mode: bool) -> Result<()> {
        let timeout = if connect_mode {
            Duration::from_millis(500)
        } else {
            HANDLER_READY_TIMEOUT
        };

        match tokio::time::timeout(timeout, ready_rx).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(_)) => anyhow::bail!("CDP handler task panicked during startup"),
            Err(_) => {
                // In connect mode, timeout is expected and harmless — the handler
                // may be busy processing existing tabs. We proceed immediately.
                if !connect_mode {
                    eprintln!(
                        "[cdp] Handler readiness timeout ({}s), proceeding cautiously",
                        timeout.as_secs()
                    );
                }
                Ok(())
            }
        }
    }

    /// Initialize network listeners, auto-attach, and page state on a browser.
    async fn init_page_state(
        browser: &Browser,
    ) -> Result<(Arc<Mutex<Page>>, Arc<Mutex<HashSet<String>>>, Arc<NetworkIdleTracker>, Vec<tokio::task::AbortHandle>)> {
        let initial_pages = cdp_op_with_timeout(browser.pages(), "pages")
            .await
            .unwrap_or_default();

        let known_ids: HashSet<String> = initial_pages
            .iter()
            .map(|p| p.target_id().as_ref().to_string())
            .collect();
        let known_pages = Arc::new(Mutex::new(known_ids));

        let new_page = cdp_op_with_timeout(browser.new_page("about:blank"), "new_page")
            .await
            .context("Failed to create browser page")?;

        let current_page = Arc::new(Mutex::new(new_page));

        {
            let p = lock_with_timeout(&current_page, "init_page").await?;
            p.execute(EnableParams::default())
                .await
                .context("Failed to enable network domain")?;

            let auto_attach = SetAutoAttachParams::builder()
                .auto_attach(true)
                .flatten(true)
                .wait_for_debugger_on_start(false)
                .build()
                .map_err(|e| anyhow::anyhow!("Failed to build SetAutoAttachParams: {}", e))?;
            p.execute(auto_attach)
                .await
                .context("Failed to enable Target.setAutoAttach")?;

            // Anti-fingerprint: inject before any page JS executes
            // This script runs on every new document (including iframes and new tabs)
            // to hide automation traces like Canvas/WebGL fingerprints, empty plugins, etc.
            if let Err(e) = p.evaluate_on_new_document(ANTI_FINGERPRINT_SCRIPT).await {
                // Non-fatal: the script may fail on some pages but the browser still works
                eprintln!("[anti-fingerprint] Script injection failed (non-fatal): {}", e);
            }
        }

        let idle_tracker = Arc::new(NetworkIdleTracker::new());
        let handles = Self::spawn_network_listeners(&current_page, &idle_tracker).await;

        Ok((current_page, known_pages, idle_tracker, handles))
    }

    /// Launch a new Chrome instance.
    pub async fn launch(profile_path: Option<&str>, show: bool) -> Result<Self> {
        win_job::init_job();

        // Anti-detection: realistic Windows Chrome User-Agent
        const USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";

        let mut builder = BrowserConfig::builder()
            .window_size(1280, 720)
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
            .arg("--disable-features=IsolateOrigins,site-per-process")
            // Anti-bot: hide navigator.webdriver flag
            .arg("--disable-blink-features=AutomationControlled")
            // Anti-bot: realistic User-Agent indistinguishable from human browser
            .arg(format!("--user-agent={}", USER_AGENT));

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
        let (mut browser, handler) = Browser::launch(config)
            .await
            .context("Failed to launch Chromium. Make sure Chrome or Edge is installed.")?;

        if let Some(child) = browser.get_mut_child() {
            let pid = child.inner.id();
            if pid > 0 {
                win_job::assign_pid(pid);
            }
        }

        let (_shutdown_tx, ready_rx) = Self::spawn_handler(handler);
        Self::wait_for_handler(ready_rx, false).await?;

        let (current_page, known_pages, idle_tracker, listener_handles) = Self::init_page_state(&browser).await?;

        Ok(Self {
            browser,
            page: current_page,
            known_pages,
            idle_tracker,
            _shutdown_tx,
            listener_handles: Mutex::new(listener_handles),
            frame_map: Arc::new(Mutex::new(HashMap::new())),
            connected: false,
        })
    }

    /// Connect to an existing Chrome instance via debugging port.
    /// This preserves ALL login sessions because the Chrome was started
    /// normally by the human user — no HMAC/Secure Preferences mismatch.
    ///
    /// The `url` should be the HTTP debugging endpoint, e.g.:
    ///   http://127.0.0.1:9222
    ///
    /// `Browser::connect` automatically fetches `/json/version` to get
    /// the WebSocket debugger URL.
    pub async fn connect(url: &str) -> Result<Self> {
        let (browser, handler) = Browser::connect(url)
            .await
            .with_context(|| format!(
                "Failed to connect to Chrome at {}. Is Chrome running with --remote-debugging-port?",
                url
            ))?;

        let (_shutdown_tx, ready_rx) = Self::spawn_handler(handler);
        Self::wait_for_handler(ready_rx, true).await?;

        let (current_page, known_pages, idle_tracker, listener_handles) = Self::init_page_state(&browser).await?;

        eprintln!("[connect] Successfully attached to existing Chrome at {}", url);
        Ok(Self {
            browser,
            page: current_page,
            known_pages,
            idle_tracker,
            _shutdown_tx,
            listener_handles: Mutex::new(listener_handles),
            frame_map: Arc::new(Mutex::new(HashMap::new())),
            connected: true,
        })
    }

    // ─── Network listeners ───────────────────────────────────────────────

    /// Spawn CDP Network event listeners on a page.
    /// Returns abort handles so callers can cancel listeners when switching tabs.
    async fn spawn_network_listeners(
        page: &Arc<Mutex<Page>>,
        tracker: &Arc<NetworkIdleTracker>,
    ) -> Vec<tokio::task::AbortHandle> {
        let page = match lock_with_timeout(page, "page").await {
            Ok(guard) => guard.clone(),
            Err(e) => {
                eprintln!("[idle] Cannot acquire page for network listeners: {}", e);
                return vec![];
            }
        };

        let mut handles = Vec::with_capacity(4);

        // R-06 fix: pass loader_id and request_id for per-loader tracking
        let tracker_req = Arc::clone(tracker);
        match page.event_listener::<EventRequestWillBeSent>().await {
            Ok(mut stream) => {
                let h = tokio::spawn(async move {
                    while let Some(event) = stream.next().await {
                        tracker_req.on_request_start(
                            event.loader_id.as_ref(),
                            event.request_id.as_ref(),
                            &event.request.url,
                            &event.r#type,
                        ).await;
                    }
                });
                handles.push(h.abort_handle());
            }
            Err(e) => eprintln!("[idle] Failed to listen for requestWillBeSent: {}", e),
        }

        let tracker_resp = Arc::clone(tracker);
        match page.event_listener::<EventResponseReceived>().await {
            Ok(mut stream) => {
                let h = tokio::spawn(async move {
                    while let Some(_event) = stream.next().await {
                        let _ = &tracker_resp;
                    }
                });
                handles.push(h.abort_handle());
            }
            Err(e) => eprintln!("[idle] Failed to listen for responseReceived: {}", e),
        }

        // R-06 fix: pass request_id for loader-id routing
        let tracker_fin = Arc::clone(tracker);
        match page.event_listener::<EventLoadingFinished>().await {
            Ok(mut stream) => {
                let h = tokio::spawn(async move {
                    while let Some(event) = stream.next().await {
                        tracker_fin.on_request_done(event.request_id.as_ref()).await;
                    }
                });
                handles.push(h.abort_handle());
            }
            Err(e) => eprintln!("[idle] Failed to listen for loadingFinished: {}", e),
        }

        let tracker_fail = Arc::clone(tracker);
        match page.event_listener::<EventLoadingFailed>().await {
            Ok(mut stream) => {
                let h = tokio::spawn(async move {
                    while let Some(event) = stream.next().await {
                        tracker_fail.on_request_done(event.request_id.as_ref()).await;
                    }
                });
                handles.push(h.abort_handle());
            }
            Err(e) => eprintln!("[idle] Failed to listen for loadingFailed: {}", e),
        }

        handles
    }

    /// Register network event listeners on a new page (e.g. after tab switch).
    /// R-01 fix: abort old listeners before spawning new ones.
    /// Returns new abort handles.
    async fn spawn_network_listeners_static(
        page: &Arc<Mutex<Page>>,
        tracker: &Arc<NetworkIdleTracker>,
    ) -> Vec<tokio::task::AbortHandle> {
        let page = match lock_with_timeout(page, "page").await {
            Ok(guard) => guard.clone(),
            Err(e) => {
                eprintln!("[idle] Cannot acquire page for network listeners: {}", e);
                return vec![];
            }
        };

        let mut handles = Vec::with_capacity(4);

        let tracker_req = Arc::clone(tracker);
        if let Ok(mut stream) = page.event_listener::<EventRequestWillBeSent>().await {
            let h = tokio::spawn(async move {
                while let Some(event) = stream.next().await {
                    tracker_req.on_request_start(
                        event.loader_id.as_ref(),
                        event.request_id.as_ref(),
                        &event.request.url,
                        &event.r#type,
                    ).await;
                }
            });
            handles.push(h.abort_handle());
        }

        let tracker_resp = Arc::clone(tracker);
        if let Ok(mut stream) = page.event_listener::<EventResponseReceived>().await {
            let h = tokio::spawn(async move {
                while let Some(_event) = stream.next().await {
                    let _ = &tracker_resp;
                }
            });
            handles.push(h.abort_handle());
        }

        let tracker_fin = Arc::clone(tracker);
        if let Ok(mut stream) = page.event_listener::<EventLoadingFinished>().await {
            let h = tokio::spawn(async move {
                while let Some(event) = stream.next().await {
                    tracker_fin.on_request_done(event.request_id.as_ref()).await;
                }
            });
            handles.push(h.abort_handle());
        }

        let tracker_fail = Arc::clone(tracker);
        if let Ok(mut stream) = page.event_listener::<EventLoadingFailed>().await {
            let h = tokio::spawn(async move {
                while let Some(event) = stream.next().await {
                    tracker_fail.on_request_done(event.request_id.as_ref()).await;
                }
            });
            handles.push(h.abort_handle());
        }

        handles
    }

    // ─── Public API ──────────────────────────────────────────────────────

    pub async fn enable_resource_blocking(&self) -> Result<()> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        page.execute(SetBlockedURLsParams { urls: blocked_urls_cached() })
            .await
            .context("Failed to set blocked URLs")?;
        Ok(())
    }

    /// Smart blocking: block only ads/tracking, allow images/CSS/fonts.
    pub async fn enable_smart_blocking(&self) -> Result<()> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        page.execute(SetBlockedURLsParams { urls: ad_only_urls_cached() })
            .await
            .context("Failed to set ad-only blocked URLs")?;
        Ok(())
    }

    pub async fn navigate(&self, url: &str) -> Result<()> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();

        // R-06 fix: start listening for the navigation's loader_id before goto.
        // We need to capture the LoaderId from the first requestWillBeSent
        // event that this navigation produces. We do this by temporarily
        // registering a one-shot listener on the page.
        // However, the simplest approach is to use the response from goto()
        // which may contain the frame/loader info. Since chromiumoxide doesn't
        // expose this directly, we set the active loader to "pending" and
        // update it when the first requestWillBeSent arrives with a new loader_id.
        //
        // For now, we clear the active loader before navigation so that
        // any events from the old page don't affect the new idle detection.
        // The first requestWillBeSent with a new loader_id will set it.
        {
            let mut active = self.idle_tracker.active_loader.lock().await;
            active.clear();
        }

        let goto_result = tokio::time::timeout(NAVIGATE_TIMEOUT, page.goto(url)).await;
        match goto_result {
            Err(_) => anyhow::bail!("Navigation to {} timed out (30s)", url),
            Ok(inner) => {
                inner.with_context(|| format!("Failed to navigate to {}", url))?;
            }
        }

        // The navigation has completed. The CDP events during navigation
        // have already populated the tracker with the new loader_id.
        // We need to find the latest loader_id from the request_counts map
        // and set it as the active loader.
        {
            let counts = self.idle_tracker.request_counts.lock().await;
            let mut active = self.idle_tracker.active_loader.lock().await;
            // The loader with the most recent requests is the active one
            if let Some((latest_loader, _)) = counts.iter().max_by_key(|(_, v)| **v) {
                *active = latest_loader.clone();
            }
        }

        self.wait_for_idle().await;
        self.detect_new_tab().await;
        Ok(())
    }

    pub async fn extract_tree(&self) -> Result<String> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();

        // Phase 0: scripts do NOT recurse into iframes (no contentDocument traversal).
        // Strategy 乙: for every frame in the frame tree, create an isolated world,
        // run mark + extract scripts, merge by tree order.

        // Step 1: Get the full frame tree
        let frame_tree = match cdp_op_with_timeout(
            page.execute(GetFrameTreeParams {}),
            "get_frame_tree",
        )
        .await
        {
            Ok(resp) => resp.result.frame_tree,
            Err(e) => {
                eprintln!("[tree] GetFrameTree failed (falling back to pages()): {}", e);
                return self.extract_tree_pages().await;
            }
        };

        // Step 2: Collect all frames in tree order
        let mut frames: Vec<FrameInfo> = Vec::new();
        collect_frames(&frame_tree, &mut frames, 0);

        if frames.is_empty() {
            eprintln!("[tree] Frame tree is empty");
            return self.extract_tree_pages().await;
        }

        // Store the frame index → frameId mapping for click/type to use.
        // This prevents index drift if the frame tree changes between extract and click.
        {
            let mut map = self.frame_map.lock().await;
            map.clear();
            for (i, frame) in frames.iter().enumerate() {
                map.insert(i, frame.frame_id.clone());
            }
        }

        // Step 3: For each frame, create isolated world and run mark + extract.
        // Each frame's elements get a unique prefix so data-agent-id is globally unique.
        // Format: f{frame_idx}-e{local_id}  (e.g., "f0-e1", "f1-e5")
        let mut all_lines: Vec<String> = Vec::new();

        const MARK_TEMPLATE: &str = r#"
        (function(prefix) {
            document.querySelectorAll('[data-agent-id]').forEach(el => {
                el.removeAttribute('data-agent-id');
            });
            const SELECTORS = [
                'button', 'a[href]', 'input', 'textarea', 'select',
                '[role="button"]', '[role="link"]', '[role="checkbox"]',
                '[role="radio"]', '[role="tab"]', '[role="menuitem"]',
                '[role="option"]', '[role="switch"]', '[role="slider"]',
                '[onclick]', '[tabindex]:not([tabindex="-1"])',
                'details', 'summary', 'label[for]'
            ];
            const candidates = new Set();
            for (const sel of SELECTORS) {
                for (const el of document.querySelectorAll(sel)) {
                    candidates.add(el);
                }
            }
            let counter = 0;
            for (const el of candidates) {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width === 0 && rect.height === 0) continue;
                if (style.display === 'none') continue;
                if (style.visibility === 'hidden') continue;
                if (parseFloat(style.opacity) === 0) continue;
                if (el.tagName !== 'BODY' && el.offsetParent === null) {
                    if (style.position !== 'fixed') continue;
                }
                counter++;
                el.setAttribute('data-agent-id', prefix + 'e' + counter);
            }
            return counter;
        })("__PREFIX__")
        "#;

        for (frame_idx, frame) in frames.iter().enumerate() {
            let prefix = format!("f{}-", frame_idx);

            // Create an isolated world for this frame
            let create_world = CreateIsolatedWorldParams::builder()
                .frame_id(frame.frame_id.clone())
                .world_name("agent_extract")
                .build()
                .map_err(|e| anyhow::anyhow!("Failed to build CreateIsolatedWorldParams: {}", e))?;

            let world_result = match cdp_op_with_timeout(
                page.execute(create_world),
                "create_isolated_world",
            )
            .await
            {
                Ok(resp) => resp.result,
                Err(e) => {
                    eprintln!("[tree] createIsolatedWorld failed for frame {}: {}", frame_idx, e);
                    continue;
                }
            };
            let ctx_id: ExecutionContextId = world_result.execution_context_id;

            // Run the mark script
            let mark_script = MARK_TEMPLATE.replace("__PREFIX__", &prefix);
            let mark_eval = EvaluateParams::builder()
                .expression(mark_script)
                .context_id(ctx_id)
                .return_by_value(true)
                .await_promise(true)
                .build()
                .map_err(|e| anyhow::anyhow!("Failed to build EvaluateParams: {}", e))?;

            let count = match tokio::time::timeout(EVALUATE_TIMEOUT, page.execute(mark_eval)).await {
                Ok(Ok(resp)) => {
                    resp.result
                        .result
                        .value
                        .and_then(|v| v.as_i64())
                        .unwrap_or(0)
                }
                Ok(Err(e)) => {
                    eprintln!("[tree] Mark evaluate failed on frame {}: {}", frame_idx, e);
                    continue;
                }
                Err(_) => {
                    eprintln!("[tree] Mark evaluate timed out on frame {}", frame_idx);
                    continue;
                }
            };

            if count == 0 {
                continue;
            }

            // Run the extract script
            let extract_eval = EvaluateParams::builder()
                .expression(EXTRACT_TREE_SCRIPT)
                .context_id(ctx_id)
                .return_by_value(true)
                .await_promise(true)
                .build()
                .map_err(|e| anyhow::anyhow!("Failed to build EvaluateParams: {}", e))?;

            let tree_result = match tokio::time::timeout(EVALUATE_TIMEOUT, page.execute(extract_eval)).await {
                Ok(Ok(resp)) => {
                    resp.result
                        .result
                        .value
                        .and_then(|v| v.as_str().map(|s| s.to_string()))
                        .unwrap_or_default()
                }
                Ok(Err(_)) => continue,
                Err(_) => continue,
            };

            if !tree_result.is_empty() {
                if frames.len() > 1 {
                    for line in tree_result.lines() {
                        all_lines.push(format!("[frame-{}] {}", frame_idx, line));
                    }
                } else {
                    all_lines.extend(tree_result.lines().map(|s| s.to_string()));
                }
            }
        }

        if all_lines.is_empty() {
            return Ok("[No interactive elements found]".to_string());
        }
        Ok(all_lines.join("\n"))
    }

    /// Fallback: extract tree using browser.pages() (same as original implementation).
    /// Used when getFrameTree is unavailable or as a compatibility fallback.
    async fn extract_tree_pages(&self) -> Result<String> {
        let all_pages = cdp_op_with_timeout(self.browser.pages(), "pages_for_tree")
            .await
            .unwrap_or_default();

        const MARK_TEMPLATE: &str = r#"
        (() => {
            document.querySelectorAll('[data-agent-id]').forEach(el => {
                el.removeAttribute('data-agent-id');
            });
            const SELECTORS = [
                'button', 'a[href]', 'input', 'textarea', 'select',
                '[role="button"]', '[role="link"]', '[role="checkbox"]',
                '[role="radio"]', '[role="tab"]', '[role="menuitem"]',
                '[role="option"]', '[role="switch"]', '[role="slider"]',
                '[onclick]', '[tabindex]:not([tabindex="-1"])',
                'details', 'summary', 'label[for]'
            ];
            const candidates = new Set();
            for (const sel of SELECTORS) {
                for (const el of document.querySelectorAll(sel)) {
                    candidates.add(el);
                }
            }
            let counter = 0;
            for (const el of candidates) {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (rect.width === 0 && rect.height === 0) continue;
                if (style.display === 'none') continue;
                if (style.visibility === 'hidden') continue;
                if (parseFloat(style.opacity) === 0) continue;
                if (el.tagName !== 'BODY' && el.offsetParent === null) {
                    if (style.position !== 'fixed') continue;
                }
                counter++;
                el.setAttribute('data-agent-id', 'e' + (counter + __OFFSET__));
            }
            return counter;
        })()
        "#;

        let mut all_lines: Vec<String> = Vec::new();
        let mut global_offset: usize = 0;

        for (frame_idx, page) in all_pages.iter().enumerate() {
            let mark_script = MARK_TEMPLATE.replace("__OFFSET__", &global_offset.to_string());

            let count = match tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(mark_script.as_str())).await {
                Ok(Ok(result)) => result.value().and_then(|v| v.as_i64()).unwrap_or(0),
                Ok(Err(e)) => {
                    eprintln!("[tree] Mark evaluate failed on frame {}: {}", frame_idx, e);
                    continue;
                }
                Err(_) => {
                    eprintln!("[tree] Mark evaluate timed out on frame {}", frame_idx);
                    continue;
                }
            };

            if count == 0 {
                continue;
            }
            global_offset += count as usize;

            let tree_result = match tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(EXTRACT_TREE_SCRIPT)).await {
                Ok(Ok(result)) => result.value().and_then(|v| v.as_str().map(|s| s.to_string())).unwrap_or_default(),
                Ok(Err(_)) => continue,
                Err(_) => continue,
            };

            if !tree_result.is_empty() {
                if all_pages.len() > 1 {
                    for line in tree_result.lines() {
                        all_lines.push(format!("[frame-{}] {}", frame_idx, line));
                    }
                } else {
                    all_lines.extend(tree_result.lines().map(|s| s.to_string()));
                }
            }
        }

        if all_lines.is_empty() {
            return Ok("[No interactive elements found]".to_string());
        }
        Ok(all_lines.join("\n"))
    }

    pub async fn click(&self, target_id: &str) -> Result<(bool, String)> {
        validate_target_id(target_id)?;
        let safe_id = escape_js_string(target_id);

        let known_before = lock_with_timeout(&self.known_pages, "known_pages")
            .await?
            .clone();

        let page = lock_with_timeout(&self.page, "page").await?.clone();

        // Decode the frame prefix from the target_id
        // Format: "f{idx}-e{local_id}" or legacy "e{id}"
        let (frame_idx_str, local_id) = if target_id.starts_with('f') {
            if let Some(dash) = target_id.find('-') {
                let idx = &target_id[1..dash];
                let local = &target_id[dash+1..];
                (Some(idx.to_string()), local.to_string())
            } else {
                (None, target_id.to_string())
            }
        } else {
            (None, target_id.to_string())
        };

        // Find the target frameId either from the frame tree or from browser.pages()
        let (target_frame_id, scrolled) = if let Some(ref fidx) = frame_idx_str {
            // Frame-prefixed: get the frame tree, find the frame at idx
            let frame_idx: usize = fidx.parse().unwrap_or(0);
            let frame_id = self.get_frame_id_at_index(frame_idx).await?;
            let scrolled = self.scroll_in_frame(&frame_id, &local_id).await?;
            (Some(frame_id), scrolled)
        } else {
            // Legacy format: try to find in browser.pages()
            let scrolled = self.smart_scroll(&safe_id).await?;
            (None, scrolled)
        };

        // Execute the click script
        let click_script = format!(
            r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id="${{id}}"]`); if (!el) return false; el.scrollIntoView({{ block: 'center', behavior: 'instant' }}); el.focus(); el.click(); return true; }})()"#,
            safe_id
        );

        let clicked = if let Some(ref frame_id) = target_frame_id {
            // Execute in the frame's isolated world
            let ctx_id = self.create_world_for_frame(frame_id).await?;
            let eval_params = EvaluateParams::builder()
                .expression(click_script)
                .context_id(ctx_id)
                .return_by_value(true)
                .await_promise(true)
                .build()
                .map_err(|e| anyhow::anyhow!("Failed to build EvaluateParams: {}", e))?;
            match tokio::time::timeout(EVALUATE_TIMEOUT, page.execute(eval_params)).await {
                Ok(Ok(resp)) => resp.result.result.value.and_then(|v| v.as_bool()).unwrap_or(false),
                _ => false,
            }
        } else {
            // Legacy: find in browser.pages()
            let all_pages = cdp_op_with_timeout(self.browser.pages(), "pages_for_click")
                .await
                .unwrap_or_default();

            let find_script = format!(
                r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id="${{id}}"]`); return !!el; }})()"#,
                safe_id
            );

            let mut target_page: Option<Page> = None;
            for p in &all_pages {
                match tokio::time::timeout(EVALUATE_TIMEOUT, p.evaluate(find_script.as_str())).await {
                    Ok(Ok(result)) => {
                        if result.value().and_then(|v| v.as_bool()).unwrap_or(false) {
                            target_page = Some(p.clone());
                            break;
                        }
                    }
                    _ => continue,
                }
            }

            match target_page {
                Some(p) => {
                    evaluate_with_timeout(&p, &click_script)
                        .await?
                        .value()
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false)
                }
                None => anyhow::bail!("Element [{}] not found in any frame", safe_id),
            }
        };

        if !clicked {
            anyhow::bail!("Element [{}] found but click failed", safe_id);
        }

        self.wait_for_idle().await;
        self.detect_new_tab_with_known(&known_before).await;
        let tree = self.extract_tree().await?;
        Ok((scrolled, tree))
    }

    pub async fn type_text(&self, target_id: &str, text: &str) -> Result<String> {
        validate_target_id(target_id)?;
        validate_text(text)?;
        let safe_id = escape_js_string(target_id);

        let page = lock_with_timeout(&self.page, "page").await?.clone();

        // Decode frame prefix (same as click)
        let (target_frame_id, _) = if target_id.starts_with('f') {
            if let Some(dash) = target_id.find('-') {
                let idx_part = &target_id[1..dash];
                let idx: usize = idx_part.parse().unwrap_or(0);
                let frame_id = self.get_frame_id_at_index(idx).await?;
                (Some(frame_id), ())
            } else {
                (None, ())
            }
        } else {
            (None, ())
        };

        let focus_script = format!(
            r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id="${{id}}"]`); if (!el) return false; el.scrollIntoView({{ block: 'center', behavior: 'instant' }}); el.focus(); if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{ el.value = ''; el.dispatchEvent(new Event('input', {{ bubbles: true }})); }} return true; }})()"#,
            safe_id
        );

        let focused = if let Some(ref frame_id) = target_frame_id {
            let ctx_id = self.create_world_for_frame(frame_id).await?;
            let eval_params = EvaluateParams::builder()
                .expression(focus_script)
                .context_id(ctx_id)
                .return_by_value(true)
                .await_promise(true)
                .build()
                .map_err(|e| anyhow::anyhow!("Failed to build EvaluateParams: {}", e))?;
            match tokio::time::timeout(EVALUATE_TIMEOUT, page.execute(eval_params)).await {
                Ok(Ok(resp)) => resp.result.result.value.and_then(|v| v.as_bool()).unwrap_or(false),
                _ => false,
            }
        } else {
            // Legacy: find in browser.pages()
            let all_pages = cdp_op_with_timeout(self.browser.pages(), "pages_for_type")
                .await
                .unwrap_or_default();
            let mut target_page: Option<Page> = None;
            for p in &all_pages {
                match tokio::time::timeout(EVALUATE_TIMEOUT, p.evaluate(focus_script.as_str())).await {
                    Ok(Ok(result)) => {
                        if result.value().and_then(|v| v.as_bool()).unwrap_or(false) {
                            target_page = Some(p.clone());
                            break;
                        }
                    }
                    _ => continue,
                }
            }
            match target_page {
                Some(p) => {
                    evaluate_with_timeout(&p, &focus_script)
                        .await?
                        .value()
                        .and_then(|v| v.as_bool())
                        .unwrap_or(false)
                }
                None => anyhow::bail!("Element [{}] not found for typing in any frame", safe_id),
            }
        };

        if !focused {
            anyhow::bail!("Element [{}] not found for typing in any frame", safe_id);
        }

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

        let delays_json = serde_json::to_string(&delays).context("Failed to serialize delays")?;

        let type_script = HUMAN_TYPE_SCRIPT
            .replace("__AGENT_ID__", &safe_id)
            .replace("__AGENT_CHARS__", &format!("[{}]", char_array))
            .replace("__AGENT_DELAYS__", &delays_json);

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
        let value = result.value();

        let scrolled = value
            .and_then(|v| v.get("scrolled").and_then(|s| s.as_bool()))
            .unwrap_or(false);

        let cross_frame = value
            .and_then(|v| v.get("crossFrame").and_then(|s| s.as_bool()))
            .unwrap_or(false);

        let cross_origin_fallback = value
            .and_then(|v| v.get("crossOriginFallback").and_then(|s| s.as_bool()))
            .unwrap_or(false);

        if cross_origin_fallback {
            // Cross-origin iframe: JS couldn't access parent frame.
            // Fall back to CDP: scroll the iframe element in the main page.
            self.scroll_iframe_into_view_via_cdp(safe_agent_id).await;
        }

        if scrolled {
            // Dynamic delay: cross-frame scrolling involves multiple viewports
            // and CSS animations, needs more time on Windows Chromium.
            let delay = if cross_frame { 700 } else { 500 };
            tokio::time::sleep(Duration::from_millis(delay)).await;
        }

        Ok(scrolled)
    }

    /// Cross-origin fallback: use CDP to scroll the iframe element
    /// into view within the main page when JS cannot access parent frames.
    async fn scroll_iframe_into_view_via_cdp(&self, safe_agent_id: &str) {
        // Get all pages; find the one that contains our target element
        let all_pages = match cdp_op_with_timeout(self.browser.pages(), "pages_for_scroll").await {
            Ok(pages) => pages,
            Err(_) => return,
        };

        // For each page, try to find an iframe that contains our target
        // by checking if the page has a frame with our target_id
        for page in &all_pages {
            let scroll_parent_script = format!(
                r#"
                (() => {{
                    // Find all iframes in this document
                    const iframes = document.querySelectorAll('iframe');
                    for (const iframe of iframes) {{
                        try {{
                            const doc = iframe.contentDocument;
                            if (doc && doc.querySelector('[data-agent-id="{}"]')) {{
                                iframe.scrollIntoView({{ block: 'center', behavior: 'smooth' }});
                                return true;
                            }}
                        }} catch (e) {{
                            // Cross-origin — skip
                        }}
                    }}
                    return false;
                }})()
                "#,
                safe_agent_id
            );

            match tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(scroll_parent_script.as_str())).await {
                Ok(Ok(result)) => {
                    if result.value().and_then(|v| v.as_bool()).unwrap_or(false) {
                        break;
                    }
                }
                _ => continue,
            }
        }
    }

    /// Get the frameId at a given index from the stored snapshot.
    /// This is safer than re-querying GetFrameTree because the frame tree
    /// may change between extract_tree and click (lazy-load iframes, ads, etc.).
    async fn get_frame_id_at_index(&self, idx: usize) -> Result<FrameId> {
        let map = self.frame_map.lock().await;
        map.get(&idx)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!(
                "Frame index {} not in snapshot. The page may have changed since the last tree extraction. Please re-extract the tree.",
                idx
            ))
    }

    /// Create an isolated world for a frame and return the execution context ID.
    async fn create_world_for_frame(&self, frame_id: &FrameId) -> Result<ExecutionContextId> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let create_world = CreateIsolatedWorldParams::builder()
            .frame_id(frame_id.clone())
            .world_name("agent_click")
            .build()
            .map_err(|e| anyhow::anyhow!("Failed to build CreateIsolatedWorldParams: {}", e))?;
        let resp = cdp_op_with_timeout(page.execute(create_world), "create_isolated_world").await?;
        Ok(resp.result.execution_context_id)
    }

    /// Scroll an element into view within a specific frame.
    async fn scroll_in_frame(&self, frame_id: &FrameId, local_id: &str) -> Result<bool> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let ctx_id = self.create_world_for_frame(frame_id).await?;
        let safe_local = escape_js_string(local_id);
        let scroll_script = format!(
            r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id$="-${{id}}"]`) || document.querySelector(`[data-agent-id="${{id}}"]`); if (!el) return false; el.scrollIntoView({{ block: 'center', behavior: 'instant' }}); return true; }})()"#,
            safe_local
        );
        let eval_params = EvaluateParams::builder()
            .expression(scroll_script)
            .context_id(ctx_id)
            .return_by_value(true)
            .await_promise(true)
            .build()
            .map_err(|e| anyhow::anyhow!("Failed to build EvaluateParams: {}", e))?;
        match tokio::time::timeout(EVALUATE_TIMEOUT, page.execute(eval_params)).await {
            Ok(Ok(resp)) => Ok(resp.result.result.value.and_then(|v| v.as_bool()).unwrap_or(false)),
            _ => Ok(false),
        }
    }

    pub async fn get_page_meta(&self) -> Result<serde_json::Value> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let result = evaluate_with_timeout(&page, PAGE_META_SCRIPT).await?;
        Ok(result.value().cloned().unwrap_or(serde_json::Value::Null))
    }

    /// Extract page body content (readable text, not just interactive elements).
    /// Uses a Readability-style algorithm to find the main content area.
    pub async fn extract_content(&self) -> Result<serde_json::Value> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let result = evaluate_with_timeout(&page, EXTRACT_CONTENT_SCRIPT).await?;
        let value = result.value().cloned().unwrap_or(serde_json::Value::Null);
        Ok(value)
    }

    /// Wait for an element to appear on the page, by target_id, text content, or CSS selector.
    /// Returns the element's target_id if found, or an error if timed out.
    pub async fn wait_for_element(
        &self,
        by: &str,
        value: &str,
        timeout_ms: u64,
    ) -> Result<serde_json::Value> {
        let query = serde_json::json!({
            "by": by,
            "value": value,
        });
        let query_str = serde_json::to_string(&query)
            .context("Failed to serialize wait query")?;

        let script = WAIT_FOR_ELEMENT_SCRIPT
            .replace("__AGENT_QUERY__", &query_str)
            .replace("__AGENT_TIMEOUT__", &timeout_ms.to_string());

        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let result = evaluate_with_timeout(&page, &script).await?;
        let value = result.value().cloned().unwrap_or(serde_json::Value::Null);

        // Check if the element was found
        let found = value.as_object()
            .and_then(|m| m.get("found"))
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        if !found {
            anyhow::bail!(
                "Element not found within {}ms: by='{}', value='{}'",
                timeout_ms, by, value
            );
        }

        Ok(value)
    }

    /// Assert that an element identified by target_id contains the expected text.
    /// Returns { passed, actual, target_id }.
    pub async fn assert_element(
        &self,
        target_id: &str,
        expected_text: &str,
    ) -> Result<serde_json::Value> {
        validate_target_id(target_id)?;

        let safe_id = escape_js_string(target_id);
        let safe_text = escape_js_string(expected_text);

        let script = ASSERT_ELEMENT_SCRIPT
            .replace("__AGENT_TARGET_ID__", &safe_id)
            .replace("__AGENT_EXPECTED_TEXT__", &safe_text);

        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let result = evaluate_with_timeout(&page, &script).await?;
        let value = result.value().cloned().unwrap_or(serde_json::Value::Null);

        Ok(value)
    }

    // ─── Download support ──────────────────────────────────────────────────

    /// Enable download behavior on the browser.
    /// Sets the download path and allows downloads to proceed.
    /// After this, clicking a download link will save the file to the specified path.
    pub async fn enable_download(&self, download_path: &str) -> Result<()> {
        let params = SetDownloadBehaviorParams {
            behavior: SetDownloadBehaviorBehavior::Allow,
            browser_context_id: None,
            download_path: Some(download_path.to_string()),
            events_enabled: Some(true),
        };

        // Execute on the browser, not on a page
        cdp_op_with_timeout(
            self.browser.execute(params),
            "enable_download",
        )
        .await
        .context("Failed to set download behavior")?;

        eprintln!("[download] Enabled download to: {}", download_path);
        Ok(())
    }

    /// Click an element that triggers a file download, then wait for the download
    /// to complete. Returns the download result with file info.
    ///
    /// This requires `enable_download` to have been called first with a valid path.
    pub async fn click_and_download(
        &self,
        target_id: &str,
        download_path: &str,
        timeout_ms: u64,
    ) -> Result<serde_json::Value> {
        // First ensure download is enabled
        self.enable_download(download_path).await?;

        // Listen for download progress events on the browser
        let (download_tx, mut download_rx) = tokio::sync::mpsc::channel::<serde_json::Value>(1);

        // Register a one-shot listener for the next download progress event
        let dl_tx = download_tx.clone();
        match self.browser.event_listener::<chromiumoxide::cdp::browser_protocol::browser::EventDownloadProgress>().await {
            Ok(mut stream) => {
                tokio::spawn(async move {
                    while let Some(event) = stream.next().await {
                        let state = event.state.as_ref().to_string();
                        let info = serde_json::json!({
                            "guid": event.guid,
                            "total_bytes": event.total_bytes,
                            "received_bytes": event.received_bytes,
                            "state": state,
                        });
                        let _ = dl_tx.send(info).await;

                        // Stop listening once download completes or cancels
                        if state == "completed" || state == "canceled" {
                            break;
                        }
                    }
                });
            }
            Err(e) => {
                eprintln!("[download] Failed to listen for download events: {}", e);
            }
        }

        // Now click the element that triggers the download
        let (_, _) = self.click(target_id).await?;

        // Wait for the download to complete with timeout
        let deadline = tokio::time::Instant::now() + Duration::from_millis(timeout_ms);
        let mut last_state = serde_json::Value::Null;

        loop {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                return Ok(serde_json::json!({
                    "status": "timeout",
                    "target_id": target_id,
                    "download_path": download_path,
                    "last_state": last_state,
                }));
            }

            match tokio::time::timeout(remaining, download_rx.recv()).await {
                Ok(Some(progress)) => {
                    let state = progress.get("state").and_then(|v| v.as_str()).unwrap_or("");
                    last_state = progress.clone();

                    if state == "completed" {
                        return Ok(serde_json::json!({
                            "status": "ok",
                            "target_id": target_id,
                            "download_path": download_path,
                            "guid": progress.get("guid"),
                            "total_bytes": progress.get("total_bytes"),
                        }));
                    }
                    if state == "canceled" {
                        return Ok(serde_json::json!({
                            "status": "canceled",
                            "target_id": target_id,
                            "download_path": download_path,
                            "last_state": progress,
                        }));
                    }
                }
                Ok(None) => {
                    break;
                }
                Err(_) => {
                    return Ok(serde_json::json!({
                        "status": "timeout",
                        "target_id": target_id,
                        "download_path": download_path,
                        "last_state": last_state,
                    }));
                }
            }
        }

        Ok(serde_json::json!({
            "status": "ok",
            "target_id": target_id,
            "download_path": download_path,
        }))
    }

    pub async fn close(&mut self) -> Result<()> {
        if self.connected {
            // In connect mode, we do NOT send Browser.close — that would kill
            // the user's Chrome instance. We just disconnect by dropping the
            // browser handle. The shutdown_tx drop will stop the handler task.
            eprintln!("[connect] Disconnecting from Chrome (not closing)");
            return Ok(());
        }
        cdp_op_with_timeout(self.browser.close(), "close")
            .await
            .context("Failed to close browser gracefully")?;
        Ok(())
    }

    // ─── Network idle ────────────────────────────────────────────────────

    /// R-06 fix: wait_for_idle now uses per-loader-id idle detection.
    /// Only the active loader's request count is checked, preventing
    /// stale events from old pages from triggering early idle.
    async fn wait_for_idle(&self) {
        if self.idle_tracker.is_idle().await {
            tokio::time::sleep(IDLE_DEBOUNCE).await;
            if self.idle_tracker.is_idle().await {
                return;
            }
        }

        let deadline = tokio::time::Instant::now() + IDLE_TIMEOUT;
        loop {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                eprintln!("[idle] Network idle timeout ({}s)", IDLE_TIMEOUT.as_secs());
                break;
            }
            if tokio::time::timeout(remaining, self.idle_tracker.notify.notified())
                .await
                .is_err()
            {
                eprintln!("[idle] Network idle timeout ({}s)", IDLE_TIMEOUT.as_secs());
                break;
            }
            tokio::time::sleep(IDLE_DEBOUNCE).await;
            if self.idle_tracker.is_idle().await {
                break;
            }
        }
    }

    // ─── Tab management ──────────────────────────────────────────────────

    async fn detect_new_tab(&self) {
        let current_pages = match cdp_op_with_timeout(self.browser.pages(), "pages").await {
            Ok(pages) => pages,
            Err(e) => {
                eprintln!("[tabs] Cannot get page list: {}", e);
                return;
            }
        };

        let current_ids: HashSet<String> = current_pages
            .iter()
            .map(|p| p.target_id().as_ref().to_string())
            .collect();

        // Hermes #8 fix: collect ALL new tab IDs, not just the first one.
        let new_tab_ids: Vec<String> = {
            let mut known = match lock_with_timeout(&self.known_pages, "known_pages").await {
                Ok(guard) => guard,
                Err(e) => {
                    eprintln!("[tabs] Cannot acquire known_pages lock: {}", e);
                    return;
                }
            };
            let new_ids: Vec<String> = current_ids.difference(&*known).cloned().collect();
            *known = current_ids;
            new_ids
        };

        // Focus the last new tab (most likely the one the user cares about)
        if let Some(new_id) = new_tab_ids.last() {
            if let Some(new_page) = current_pages
                .iter()
                .find(|p| p.target_id().as_ref() == new_id.as_str())
            {
                eprintln!("[tabs] Switching focus to new tab: {}", new_id);
                if let Ok(mut active) = lock_with_timeout(&self.page, "page").await {
                    *active = new_page.clone();
                }
                // R-01 fix: abort old listeners before spawning new ones
                if let Ok(mut handles) = lock_with_timeout(&self.listener_handles, "listener_handles").await {
                    for h in handles.drain(..) {
                        h.abort();
                    }
                    let new_handles = Self::spawn_network_listeners_static(&self.page, &self.idle_tracker).await;
                    *handles = new_handles;
                }
            }
        }
    }

    async fn detect_new_tab_with_known(&self, known_before: &HashSet<String>) {
        let current_pages = match cdp_op_with_timeout(self.browser.pages(), "pages").await {
            Ok(pages) => pages,
            Err(e) => {
                eprintln!("[tabs] Cannot get page list: {}", e);
                return;
            }
        };

        let current_ids: HashSet<String> = current_pages
            .iter()
            .map(|p| p.target_id().as_ref().to_string())
            .collect();

        // Hermes #8 fix: collect ALL new tab IDs, not just the first one.
        let new_tab_ids: Vec<String> = current_ids.difference(known_before).cloned().collect();

        // Focus the last new tab (most likely the one the user cares about)
        if let Some(new_id) = new_tab_ids.last() {
            if let Some(new_page) = current_pages
                .iter()
                .find(|p| p.target_id().as_ref() == new_id.as_str())
            {
                eprintln!("[tabs] Switching focus to new tab: {}", new_id);
                if let Ok(mut active) = lock_with_timeout(&self.page, "page").await {
                    *active = new_page.clone();
                }
                // R-01 fix: abort old listeners before spawning new ones
                if let Ok(mut handles) = lock_with_timeout(&self.listener_handles, "listener_handles").await {
                    for h in handles.drain(..) {
                        h.abort();
                    }
                    let new_handles = Self::spawn_network_listeners_static(&self.page, &self.idle_tracker).await;
                    *handles = new_handles;
                }
            }
        }

        if let Ok(mut known) = lock_with_timeout(&self.known_pages, "known_pages").await {
            *known = current_ids;
        }
    }
}

/// Information about a frame in the frame tree.
struct FrameInfo {
    frame_id: FrameId,
    #[allow(dead_code)]
    depth: usize,
}

/// Recursively collect frames from a FrameTree in tree order.
fn collect_frames(tree: &FrameTree, frames: &mut Vec<FrameInfo>, depth: usize) {
    frames.push(FrameInfo {
        frame_id: tree.frame.id.clone(),
        depth,
    });
    if let Some(ref children) = tree.child_frames {
        for child in children {
            collect_frames(child, frames, depth + 1);
        }
    }
}
