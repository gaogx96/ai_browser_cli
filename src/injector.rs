/// JavaScript injection scripts for AI agent browser interaction.
///
/// All scripts are self-contained IIFEs that do not pollute the global scope.
/// External parameters are passed through placeholder tokens that MUST be
/// sanitized by the Rust caller before injection (see `utils::escape_js_string`).
///
/// M-05 fix: INJECT_MARK_SCRIPT has been moved inline into browser.rs's
/// extract_tree() to support a global offset parameter for multi-frame
/// element numbering (e.g., frame-0 elements: e1-e50, frame-1: e51-e100).

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

// M-02 fix: WAIT_FOR_IDLE_SCRIPT removed.
// Network idle detection now uses CDP Network domain events in Rust,
// which cannot be spoofed by page JavaScript. See browser.rs NetworkIdleTracker.
