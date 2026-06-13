use anyhow::{Context, Result};
use chromiumoxide::browser::{Browser, BrowserConfig};
use chromiumoxide::cdp::browser_protocol::network::{
    EnableParams, EventLoadingFailed, EventLoadingFinished, EventRequestWillBeSent,
    EventResponseReceived, SetBlockedUrLsParams as SetBlockedURLsParams,
};
use chromiumoxide::cdp::browser_protocol::target::SetAutoAttachParams;
use chromiumoxide::page::Page;
use futures::StreamExt;
use rand::Rng;
use std::collections::HashSet;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, oneshot, Mutex, Notify};

use crate::injector::{
    EXTRACT_TREE_SCRIPT, HUMAN_TYPE_SCRIPT, PAGE_META_SCRIPT, smart_scroll_script,
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
const CDP_OP_TIMEOUT: Duration = Duration::from_secs(10);
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
    let bytes = id.as_bytes();
    if bytes[0] != b'e' || !bytes[1..].iter().all(|b| b.is_ascii_digit()) {
        anyhow::bail!(
            "target_id has invalid format: '{}'. Expected 'e' followed by digits (e.g., 'e5')",
            id
        );
    }
    Ok(())
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
    "alimama.com", "tanx.com", "atpanel.com", "yimg.com",
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
    // Hotjar / Clarity
    "hotjar.com", "clarity.ms",
    // Sentry / NewRelic
    "sentry.io", "ingest.sentry.io",
    "newrelic.com", "nr-data.net", "bam.nr-data.net",
    // Bing
    "bat.bing.com", "clarity.ms",
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

struct NetworkIdleTracker {
    active_count: AtomicUsize,
    notify: Notify,
}

impl NetworkIdleTracker {
    fn new() -> Self {
        Self {
            active_count: AtomicUsize::new(0),
            notify: Notify::new(),
        }
    }

    fn on_request_start(&self, url: &str, resource_type: &Option<ResourceType>) {
        if should_skip_for_idle(url, resource_type) {
            return;
        }
        self.active_count.fetch_add(1, Ordering::SeqCst);
    }

    fn on_request_done(&self) {
        let prev = self.active_count.fetch_update(
            Ordering::SeqCst,
            Ordering::SeqCst,
            |current| Some(current.saturating_sub(1)),
        );
        if let Ok(1) = prev {
            self.notify.notify_one();
        }
    }

    fn is_idle(&self) -> bool {
        self.active_count.load(Ordering::SeqCst) == 0
    }
}

// ═══════════════════════════════════════════════════════════════════════
// Browser state
// ═══════════════════════════════════════════════════════════════════════

pub struct BrowserState {
    pub browser: Browser,
    pub page: Arc<Mutex<Page>>,
    known_pages: Arc<Mutex<HashSet<String>>>,
    idle_tracker: Arc<NetworkIdleTracker>,
    _shutdown_tx: mpsc::Sender<()>,
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
    ) -> Result<(Arc<Mutex<Page>>, Arc<Mutex<HashSet<String>>>, Arc<NetworkIdleTracker>)> {
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
        }

        let idle_tracker = Arc::new(NetworkIdleTracker::new());
        Self::spawn_network_listeners(&current_page, &idle_tracker).await;

        Ok((current_page, known_pages, idle_tracker))
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

        let (current_page, known_pages, idle_tracker) = Self::init_page_state(&browser).await?;

        Ok(Self {
            browser,
            page: current_page,
            known_pages,
            idle_tracker,
            _shutdown_tx,
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

        let (current_page, known_pages, idle_tracker) = Self::init_page_state(&browser).await?;

        eprintln!("[connect] Successfully attached to existing Chrome at {}", url);
        Ok(Self {
            browser,
            page: current_page,
            known_pages,
            idle_tracker,
            _shutdown_tx,
            connected: true,
        })
    }

    // ─── Network listeners ───────────────────────────────────────────────

    /// Spawn CDP Network event listeners on a page.
    async fn spawn_network_listeners(
        page: &Arc<Mutex<Page>>,
        tracker: &Arc<NetworkIdleTracker>,
    ) {
        let page = match lock_with_timeout(page, "page").await {
            Ok(guard) => guard.clone(),
            Err(e) => {
                eprintln!("[idle] Cannot acquire page for network listeners: {}", e);
                return;
            }
        };

        // Request start — increment counter (with multi-dimensional filtering)
        let tracker_req = Arc::clone(tracker);
        match page.event_listener::<EventRequestWillBeSent>().await {
            Ok(mut stream) => {
                tokio::spawn(async move {
                    while let Some(event) = stream.next().await {
                        tracker_req.on_request_start(&event.request.url, &event.r#type);
                    }
                });
            }
            Err(e) => eprintln!("[idle] Failed to listen for requestWillBeSent: {}", e),
        }

        // responseReceived — no counter change (fires on headers, not body complete)
        let tracker_resp = Arc::clone(tracker);
        match page.event_listener::<EventResponseReceived>().await {
            Ok(mut stream) => {
                tokio::spawn(async move {
                    while let Some(_event) = stream.next().await {
                        let _ = &tracker_resp;
                    }
                });
            }
            Err(e) => eprintln!("[idle] Failed to listen for responseReceived: {}", e),
        }

        // Loading finished — decrement counter
        let tracker_fin = Arc::clone(tracker);
        match page.event_listener::<EventLoadingFinished>().await {
            Ok(mut stream) => {
                tokio::spawn(async move {
                    while let Some(_event) = stream.next().await {
                        tracker_fin.on_request_done();
                    }
                });
            }
            Err(e) => eprintln!("[idle] Failed to listen for loadingFinished: {}", e),
        }

        // Loading failed — decrement counter
        let tracker_fail = Arc::clone(tracker);
        match page.event_listener::<EventLoadingFailed>().await {
            Ok(mut stream) => {
                tokio::spawn(async move {
                    while let Some(_event) = stream.next().await {
                        tracker_fail.on_request_done();
                    }
                });
            }
            Err(e) => eprintln!("[idle] Failed to listen for loadingFailed: {}", e),
        }
    }

    fn spawn_network_listeners_static(
        page: &Arc<Mutex<Page>>,
        tracker: &Arc<NetworkIdleTracker>,
    ) {
        let page = match page.try_lock() {
            Ok(guard) => guard.clone(),
            Err(_) => return,
        };
        let tracker_req = Arc::clone(tracker);
        if let Ok(mut stream) = futures::executor::block_on(
            page.event_listener::<EventRequestWillBeSent>(),
        ) {
            tokio::spawn(async move {
                while let Some(event) = stream.next().await {
                    tracker_req.on_request_start(&event.request.url, &event.r#type);
                }
            });
        }
        let tracker_fin = Arc::clone(tracker);
        if let Ok(mut stream) = futures::executor::block_on(
            page.event_listener::<EventLoadingFinished>(),
        ) {
            tokio::spawn(async move {
                while let Some(_event) = stream.next().await {
                    tracker_fin.on_request_done();
                }
            });
        }
        let tracker_fail = Arc::clone(tracker);
        if let Ok(mut stream) = futures::executor::block_on(
            page.event_listener::<EventLoadingFailed>(),
        ) {
            tokio::spawn(async move {
                while let Some(_event) = stream.next().await {
                    tracker_fail.on_request_done();
                }
            });
        }
    }

    // ─── Public API ──────────────────────────────────────────────────────

    pub async fn enable_resource_blocking(&self) -> Result<()> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        page.execute(SetBlockedURLsParams { urls: blocked_urls_cached() })
            .await
            .context("Failed to set blocked URLs")?;
        Ok(())
    }

    pub async fn navigate(&self, url: &str) -> Result<()> {
        let page = lock_with_timeout(&self.page, "page").await?.clone();
        let goto_result = tokio::time::timeout(NAVIGATE_TIMEOUT, page.goto(url)).await;
        match goto_result {
            Err(_) => anyhow::bail!("Navigation to {} timed out (30s)", url),
            Ok(inner) => {
                inner.with_context(|| format!("Failed to navigate to {}", url))?;
            }
        }
        self.wait_for_idle().await;
        self.detect_new_tab().await;
        Ok(())
    }

    pub async fn extract_tree(&self) -> Result<String> {
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
                Ok(Err(_)) => continue,
                Err(_) => continue,
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
        let scrolled = self.smart_scroll(&safe_id).await?;

        let known_before = lock_with_timeout(&self.known_pages, "known_pages")
            .await?
            .clone();

        let find_script = format!(
            r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id="${{id}}"]`); return !!el; }})()"#,
            safe_id
        );
        let click_script = format!(
            r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id="${{id}}"]`); if (!el) return false; el.scrollIntoView({{ block: 'center', behavior: 'instant' }}); el.focus(); el.click(); return true; }})()"#,
            safe_id
        );

        let all_pages = cdp_op_with_timeout(self.browser.pages(), "pages_for_click")
            .await
            .unwrap_or_default();

        let mut target_page: Option<Page> = None;
        for page in &all_pages {
            match tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(find_script.as_str())).await {
                Ok(Ok(result)) => {
                    if result.value().and_then(|v| v.as_bool()).unwrap_or(false) {
                        target_page = Some(page.clone());
                        break;
                    }
                }
                _ => continue,
            }
        }

        let page = match target_page {
            Some(p) => p,
            None => anyhow::bail!("Element [{}] not found in any frame", safe_id),
        };

        let clicked = evaluate_with_timeout(&page, &click_script)
            .await?
            .value()
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

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
        self.smart_scroll(&safe_id).await?;

        let focus_script = format!(
            r#"(() => {{ const id = CSS.escape("{}"); const el = document.querySelector(`[data-agent-id="${{id}}"]`); if (!el) return false; el.scrollIntoView({{ block: 'center', behavior: 'instant' }}); el.focus(); if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{ el.value = ''; el.dispatchEvent(new Event('input', {{ bubbles: true }})); }} return true; }})()"#,
            safe_id
        );

        let all_pages = cdp_op_with_timeout(self.browser.pages(), "pages_for_type")
            .await
            .unwrap_or_default();

        let mut target_page: Option<Page> = None;
        for page in &all_pages {
            match tokio::time::timeout(EVALUATE_TIMEOUT, page.evaluate(focus_script.as_str())).await {
                Ok(Ok(result)) => {
                    if result.value().and_then(|v| v.as_bool()).unwrap_or(false) {
                        target_page = Some(page.clone());
                        break;
                    }
                }
                _ => continue,
            }
        }

        let page = match target_page {
            Some(p) => p,
            None => anyhow::bail!("Element [{}] not found for typing in any frame", safe_id),
        };

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
        Ok(result.value().cloned().unwrap_or(serde_json::Value::Null))
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

    async fn wait_for_idle(&self) {
        if self.idle_tracker.is_idle() {
            tokio::time::sleep(IDLE_DEBOUNCE).await;
            if self.idle_tracker.is_idle() {
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
            if self.idle_tracker.is_idle() {
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

        let new_tab_id = {
            let mut known = match lock_with_timeout(&self.known_pages, "known_pages").await {
                Ok(guard) => guard,
                Err(e) => {
                    eprintln!("[tabs] Cannot acquire known_pages lock: {}", e);
                    return;
                }
            };
            let new_id = current_ids.difference(&*known).next().cloned();
            *known = current_ids;
            new_id
        };

        if let Some(new_id) = &new_tab_id {
            if let Some(new_page) = current_pages
                .iter()
                .find(|p| p.target_id().as_ref() == new_id.as_str())
            {
                eprintln!("[tabs] Switching focus to new tab: {}", new_id);
                if let Ok(mut active) = lock_with_timeout(&self.page, "page").await {
                    *active = new_page.clone();
                }
                Self::spawn_network_listeners_static(&self.page, &self.idle_tracker);
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
                Self::spawn_network_listeners_static(&self.page, &self.idle_tracker);
            }
        }

        if let Ok(mut known) = lock_with_timeout(&self.known_pages, "known_pages").await {
            *known = current_ids;
        }
    }
}
