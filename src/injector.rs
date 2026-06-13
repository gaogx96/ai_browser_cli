/// JavaScript injection scripts for AI agent browser interaction.
///
/// All scripts are self-contained IIFEs that do not pollute the global scope.
/// External parameters are passed through placeholder tokens that MUST be
/// sanitized by the Rust caller before injection (see `utils::escape_js_string`).

/// Mark all visible interactive elements with unique `data-agent-id` attributes.
///
/// Visibility filter logic:
/// 1. `getBoundingClientRect()` — zero-size check
/// 2. `getComputedStyle()` — display/visibility/opacity
/// 3. `offsetParent` — BUT fixed-position elements are explicitly allowed
///    (offsetParent is null for fixed elements, which the old code incorrectly skipped)
pub const INJECT_MARK_SCRIPT: &str = r#"
(() => {
    // Remove previous marks
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

        // Filter hidden elements
        if (rect.width === 0 && rect.height === 0) continue;
        if (style.display === 'none') continue;
        if (style.visibility === 'hidden') continue;
        if (parseFloat(style.opacity) === 0) continue;

        // offsetParent is null for:
        //   - position: fixed (VALID — must not skip)
        //   - display: none (already caught above)
        //   - body element
        // So we only skip if offsetParent is null AND position is NOT fixed
        if (el.tagName !== 'BODY' && el.offsetParent === null) {
            if (style.position !== 'fixed') continue;
        }

        counter++;
        el.setAttribute('data-agent-id', 'e' + counter);
    }

    return counter;
})()
"#;

/// Extract a simplified accessibility tree from marked elements.
///
/// Returns a multi-line string where each line is:
///   [@eN] role "text"
///
/// Text is extracted from: textContent, placeholder, aria-label, title, alt, value.
/// Truncated to 50 chars max. Empty text omitted.
pub const EXTRACT_TREE_SCRIPT: &str = r#"
(() => {
    const elements = document.querySelectorAll('[data-agent-id]');
    const lines = [];

    for (const el of elements) {
        const id = el.getAttribute('data-agent-id');

        // Determine role
        let role = el.getAttribute('role') || '';
        if (!role) {
            const tag = el.tagName.toLowerCase();
            const roleMap = {
                'a': 'link', 'button': 'button', 'input': 'input',
                'textarea': 'textbox', 'select': 'combobox',
                'h1': 'heading', 'h2': 'heading', 'h3': 'heading',
                'h4': 'heading', 'h5': 'heading', 'h6': 'heading',
                'img': 'img', 'nav': 'navigation', 'main': 'main',
                'header': 'banner', 'footer': 'contentinfo',
                'table': 'table', 'ul': 'list', 'ol': 'list',
                'li': 'listitem', 'form': 'form', 'section': 'region',
                'article': 'article', 'aside': 'complementary',
                'details': 'details', 'summary': 'button',
                'label': 'label', 'p': 'paragraph', 'div': 'group',
                'span': 'text', 'td': 'cell', 'th': 'columnheader',
                'tr': 'row'
            };
            role = roleMap[tag] || tag;
        }

        // Extract text (priority order)
        let text = '';
        const ariaLabel = el.getAttribute('aria-label');
        const placeholder = el.getAttribute('placeholder');
        const alt = el.getAttribute('alt');
        const value = el.getAttribute('value');

        if (ariaLabel) {
            text = ariaLabel;
        } else if (placeholder && ['INPUT', 'TEXTAREA'].includes(el.tagName)) {
            text = placeholder;
        } else if (el.tagName === 'IMG' && alt) {
            text = alt;
        } else if (el.tagName === 'INPUT' && value && el.type !== 'password') {
            text = value;
        } else {
            const directText = Array.from(el.childNodes)
                .filter(n => n.nodeType === Node.TEXT_NODE)
                .map(n => n.textContent.trim())
                .filter(t => t)
                .join(' ');

            if (directText) {
                text = directText;
            } else {
                text = el.textContent || '';
                text = text.replace(/\s+/g, ' ').trim();
            }
        }

        if (text.length > 50) {
            text = text.substring(0, 47) + '...';
        }

        text = text.replace(/\\/g, '\\\\').replace(/"/g, '\\"');

        let line = '[@' + id + '] ' + role;
        if (text) {
            line += ' "' + text + '"';
        }
        lines.push(line);
    }

    return lines.join('\n');
})()
"#;

/// Smart smooth scroll: check if element is in viewport, scroll to center if not.
///
/// Uses CSS.escape() + template literal for the agent-id selector.
/// Returns `{ scrolled, inView }`.
///
/// The `agent_id` value MUST be pre-escaped by the Rust caller (`escape_js_string`).
/// CSS.escape() provides a second defense layer against selector injection.
pub fn smart_scroll_script(agent_id_escaped: &str) -> String {
    format!(
        r#"
(() => {{
    const id = CSS.escape("{}");
    const el = document.querySelector(`[data-agent-id="${{id}}"]`);
    if (!el) return {{ scrolled: false, inView: false, error: 'element not found' }};

    const rect = el.getBoundingClientRect();
    const vh = window.innerHeight;
    const vw = window.innerWidth;

    const inView = (
        rect.top >= 0 && rect.left >= 0 &&
        rect.bottom <= vh && rect.right <= vw &&
        rect.height > 0 && rect.width > 0
    );

    if (inView) return {{ scrolled: false, inView: true }};

    el.scrollIntoView({{ block: 'center', inline: 'nearest', behavior: 'smooth' }});
    return {{ scrolled: true, inView: false }};
}})()
"#,
        agent_id_escaped
    )
}

/// Get the current page metadata: title, url, and basic stats.
pub const PAGE_META_SCRIPT: &str = r#"
(() => ({
    title: document.title || '',
    url: location.href,
    interactiveCount: document.querySelectorAll('[data-agent-id]').length
}))()
"#;

/// Human-like typing script — dispatches individual KeyboardEvents with per-char delays.
///
/// Placeholders (MUST be sanitized before injection):
///   __AGENT_ID__     — the data-agent-id value (e.g. "e5")
///   __AGENT_CHARS__  — JS array of characters, e.g. ['h','e','l','l','o']
///   __AGENT_DELAYS__ — JSON array of delays in ms, e.g. [32,18,41,27]
///
/// Each character dispatches: keydown → keypress → value mutation → input → keyup
/// with a randomized delay (default 30ms ± 15ms) between keystrokes.
///
/// The Promise resolves with `true` on success, `false` if the target element
/// is not found. A 30-second hard timeout prevents permanent hangs.
pub const HUMAN_TYPE_SCRIPT: &str = r#"
(() => {
    const id = CSS.escape('__AGENT_ID__');
    const el = document.querySelector(`[data-agent-id="${id}"]`);
    if (!el) return false;

    el.scrollIntoView({ block: 'center', behavior: 'instant' });
    el.focus();

    // Clear existing value
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
        el.value = '';
        el.dispatchEvent(new Event('input', { bubbles: true }));
    }

    const chars = __AGENT_CHARS__;
    const delays = __AGENT_DELAYS__;

    return new Promise((resolve) => {
        let i = 0;
        let timer = null;

        function keyCode(ch) {
            if (ch >= 'a' && ch <= 'z') return 'Key' + ch.toUpperCase();
            if (ch >= 'A' && ch <= 'Z') return 'Key' + ch;
            if (ch >= '0' && ch <= '9') return 'Digit' + ch;
            if (ch === ' ') return 'Space';
            if (ch === 'Enter') return 'Enter';
            if (ch === 'Backspace') return 'Backspace';
            if (ch === 'Tab') return 'Tab';
            return 'Key' + ch.toUpperCase();
        }

        // Hard timeout: prevent permanent hang if setTimeout is throttled
        const timeout = setTimeout(() => {
            if (timer) clearTimeout(timer);
            resolve(false);
        }, 30000);

        function typeNext() {
            if (i >= chars.length) {
                el.dispatchEvent(new Event('change', { bubbles: true }));
                clearTimeout(timeout);
                resolve(true);
                return;
            }

            const ch = chars[i];
            const delay = delays[i] || 30;
            const code = keyCode(ch);
            i++;

            el.dispatchEvent(new KeyboardEvent('keydown', {
                key: ch, code: code, bubbles: true, cancelable: true
            }));
            el.dispatchEvent(new KeyboardEvent('keypress', {
                key: ch, code: code, bubbles: true, cancelable: true
            }));

            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                el.value += ch;
            } else if (el.isContentEditable) {
                el.textContent += ch;
            }

            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new KeyboardEvent('keyup', {
                key: ch, code: code, bubbles: true, cancelable: true
            }));

            timer = setTimeout(typeNext, delay);
        }

        typeNext();
    });
})()
"#;

/// Network idle detection — IDEMPOTENT version.
///
/// Each invocation first unhooks any previous XMLHttpRequest/fetch overrides
/// before installing fresh hooks. This prevents hook accumulation across
/// multiple navigate/call cycles.
///
/// Algorithm:
/// 1. Hook XHR.send and fetch to track active request count
/// 2. When count reaches 0, start a 500ms idle timer
/// 3. If a new request starts during the idle window, cancel and reset
/// 4. Hard timeout at 10 seconds — resolves `false` to prevent permanent hang
///
/// Returns `true` if idle achieved, `false` on timeout.
pub const WAIT_FOR_IDLE_SCRIPT: &str = r#"
(() => {
    return new Promise((resolve) => {
        // ── Unhook previous installation (idempotency) ──
        if (window.__agent_idle_cleanup) {
            window.__agent_idle_cleanup();
        }

        let active = 0;
        let idleTimer = null;
        let settled = false;
        const IDLE_MS = 500;
        const TIMEOUT_MS = 10000;

        function scheduleIdle() {
            if (settled) return;
            if (idleTimer) clearTimeout(idleTimer);
            if (active <= 0) {
                idleTimer = setTimeout(() => {
                    if (!settled) { settled = true; cleanup(); resolve(true); }
                }, IDLE_MS);
            }
        }

        function onActive() {
            if (settled) return;
            active++;
            if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
        }

        function onDone() {
            if (settled) return;
            active = Math.max(0, active - 1);
            scheduleIdle();
        }

        // ── Hook XMLHttpRequest ──
        const OrigXHR = window.XMLHttpRequest;
        const origOpen = OrigXHR.prototype.open;
        const origSend = OrigXHR.prototype.send;

        OrigXHR.prototype.send = function() {
            onActive();
            this.addEventListener('loadend', onDone);
            return origSend.apply(this, arguments);
        };

        // ── Hook fetch ──
        const origFetch = window.fetch;
        window.fetch = function() {
            onActive();
            return origFetch.apply(this, arguments).then(
                r => { onDone(); return r; },
                e => { onDone(); throw e; }
            );
        };

        // ── Cleanup function for next invocation ──
        function cleanup() {
            try { OrigXHR.prototype.open = origOpen; } catch(e) {}
            try { OrigXHR.prototype.send = origSend; } catch(e) {}
            try { window.fetch = origFetch; } catch(e) {}
            if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
        }
        window.__agent_idle_cleanup = cleanup;

        // ── Start ──
        scheduleIdle();

        // Hard timeout
        setTimeout(() => {
            if (!settled) { settled = true; cleanup(); resolve(false); }
        }, TIMEOUT_MS);
    });
})()
"#;
