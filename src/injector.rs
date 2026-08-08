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
    const MAX_NODES = 300;
    const elements = document.querySelectorAll('[data-agent-id]');
    const lines = [];
    const total = elements.length;

    for (let i = 0; i < Math.min(elements.length, MAX_NODES); i++) {
        const el = elements[i];
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

    let result = lines.join('\n');
    if (total > MAX_NODES) {
        return result + '\n... (' + total + ' elements, showing first ' + MAX_NODES + ')';
    }
    return result;
})()
"#;

/// Smart smooth scroll with cross-frame upward traversal.
///
/// Algorithm:
///   1. Find the target element in the current frame and scrollIntoView.
///   2. Walk up window.parent chain, scrolling each iframe container
///      into view in its parent frame.
///   3. On cross-origin SecurityError, fall back to a flag so the Rust
///      layer can use CDP to scroll the parent frame externally.
///
/// Returns `{ scrolled, inView, crossFrame, crossOriginFallback }`.
pub fn smart_scroll_script(agent_id_escaped: &str) -> String {
    format!(
        r#"
(() => {{
    const id = CSS.escape("{}");
    const el = document.querySelector(`[data-agent-id="${{id}}"]`);
    if (!el) return {{ scrolled: false, inView: false, error: 'element not found' }};

    // ── Step 1: scroll element into view within its own frame ──
    el.scrollIntoView({{ block: 'center', inline: 'nearest', behavior: 'smooth' }});

    // ── Step 2: check if element is in the top-level viewport ──
    function isInTopViewport(elem) {{
        let rect = elem.getBoundingClientRect();
        // Walk up through frames to get cumulative offset
        let win = window;
        let top = rect.top;
        let left = rect.left;
        while (win !== win.top) {{
            try {{
                const frameEl = win.frameElement;
                if (!frameEl) break;
                const frameRect = frameEl.getBoundingClientRect();
                top += frameRect.top;
                left += frameRect.left;
                win = win.parent;
            }} catch (e) {{
                // Cross-origin — cannot compute top-level position
                break;
            }}
        }}
        return (
            top >= 0 && left >= 0 &&
            top < win.innerHeight && left < win.innerWidth &&
            rect.height > 0 && rect.width > 0
        );
    }}

    if (isInTopViewport(el)) {{
        return {{ scrolled: true, inView: true, crossFrame: false, crossOriginFallback: false }};
    }}

    // ── Step 3: walk up the frame chain, scrolling each iframe into view ──
    let currentWin = window;
    let scrolledFrames = 0;
    let crossOriginFallback = false;

    while (currentWin !== currentWin.top) {{
        try {{
            const frameEl = currentWin.frameElement;
            if (!frameEl) break;

            // Scroll the iframe element into view within the parent frame
            frameEl.scrollIntoView({{ block: 'center', inline: 'nearest', behavior: 'smooth' }});
            scrolledFrames++;
            currentWin = currentWin.parent;
        }} catch (e) {{
            // SecurityError: cross-origin iframe, cannot access frameElement
            crossOriginFallback = true;
            break;
        }}
    }}

    return {{
        scrolled: scrolledFrames > 0,
        inView: false,
        crossFrame: scrolledFrames > 0,
        crossOriginFallback: crossOriginFallback
    }};
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
            i++;

            // Hermes #13 fix: non-ASCII chars (CJK, emoji) use InputEvent
            // instead of KeyboardEvent. This correctly triggers Vue/React
            // reactivity for IME-style input without relying on code field.
            if (ch.charCodeAt(0) > 127) {
                el.dispatchEvent(new InputEvent('beforeinput', {
                    inputType: 'insertText', data: ch, bubbles: true, cancelable: true
                }));
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                    el.value += ch;
                } else if (el.isContentEditable) {
                    el.textContent += ch;
                }
                el.dispatchEvent(new InputEvent('input', {
                    inputType: 'insertText', data: ch, bubbles: true
                }));
            } else {
                const code = keyCode(ch);
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
            }

            timer = setTimeout(typeNext, delay);
        }

        typeNext();
    });
})()
"#;

// ═══════════════════════════════════════════════════════════════════════
// Content Extraction Script — Readability-style page text extraction
// ═══════════════════════════════════════════════════════════════════════
//
// Extracts the main readable content of a page using a simplified
// Readability algorithm. Falls back to clean body text if no article
// element is found. Returns structured JSON with title, content, and
// metadata.

pub const EXTRACT_CONTENT_SCRIPT: &str = r#"
(() => {
    function getNodeText(node) {
        if (!node) return '';
        if (node.nodeType === Node.TEXT_NODE) return node.textContent.trim();
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        const tag = node.tagName.toLowerCase();
        if (tag === 'script' || tag === 'style' || tag === 'noscript' ||
            tag === 'svg' || tag === 'canvas' || tag === 'iframe') return '';
        let text = '';
        for (const child of node.childNodes) {
            text += getNodeText(child) + ' ';
        }
        return text.replace(/\s+/g, ' ').trim();
    }

    function getContentScore(node) {
        if (!node || node.nodeType !== Node.ELEMENT_NODE) return 0;
        const tag = node.tagName.toLowerCase();
        if (tag === 'script' || tag === 'style' || tag === 'noscript') return 0;

        let score = 0;
        const text = getNodeText(node);
        const textLen = text.length;

        // 正文密度：文本占比越高 → 越可能是内容
        if (textLen > 50) score += Math.min(textLen / 100, 10);

        // 标签加分
        if (tag === 'article') score += 8;
        if (tag === 'main') score += 6;
        if (tag === 'section') score += 3;
        if (tag === 'p') score += 2;
        if (tag === 'pre' || tag === 'code') score += 2;

        // class/id 关键词匹配
        const cls = (node.className || '').toLowerCase();
        const id = (node.id || '').toLowerCase();
        if (cls.includes('content') || id.includes('content')) score += 5;
        if (cls.includes('article') || id.includes('article')) score += 5;
        if (cls.includes('post') || id.includes('post')) score += 4;
        if (cls.includes('main') || id.includes('main')) score += 4;
        if (cls.includes('body') || id.includes('body')) score += 2;

        // 减分：广告/侧栏/导航
        if (cls.includes('sidebar') || id.includes('sidebar')) score -= 5;
        if (cls.includes('nav') || id.includes('nav')) score -= 4;
        if (cls.includes('menu') || id.includes('menu')) score -= 4;
        if (cls.includes('footer') || id.includes('footer')) score -= 3;
        if (cls.includes('ad') || id.includes('ad')) score -= 5;
        if (cls.includes('comment') || id.includes('comment')) score -= 2;

        // 后代加分
        for (const child of node.children) {
            score += getContentScore(child) * 0.5;
        }

        return score;
    }

    function extractContent(root) {
        const candidates = [];
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null, false);
        let node;
        while (node = walker.nextNode()) {
            const tag = node.tagName.toLowerCase();
            if (tag === 'body' || tag === 'html' || tag === 'div' ||
                tag === 'article' || tag === 'section' || tag === 'main') {
                const score = getContentScore(node);
                if (score > 3) {
                    candidates.push({ node, score, text: getNodeText(node) });
                }
            }
        }

        // 按分数排序，取最高分
        candidates.sort((a, b) => b.score - a.score);
        const best = candidates[0];

        if (best && best.text.length > 100) {
            return {
                title: document.title || '',
                url: location.href,
                text: best.text,
                wordCount: best.text.split(/\s+/).filter(w => w).length,
                charCount: best.text.length,
                method: 'readability'
            };
        }

        // Fallback: 提取 body 中所有可见文本
        const body = document.body;
        if (body) {
            const text = getNodeText(body);
            if (text.length > 50) {
                return {
                    title: document.title || '',
                    url: location.href,
                    text: text,
                    wordCount: text.split(/\s+/).filter(w => w).length,
                    charCount: text.length,
                    method: 'fallback'
                };
            }
        }

        return {
            title: document.title || '',
            url: location.href,
            text: '',
            wordCount: 0,
            charCount: 0,
            method: 'empty'
        };
    }

    return extractContent(document);
})();
"#;

// ═══════════════════════════════════════════════════════════════════════
// Wait-For-Element Script — poll for an element to appear
// ═══════════════════════════════════════════════════════════════════════
//
// Polls for an element matching the given criteria (by text content or
// target_id) and returns its target_id once found. Used by the wait_for
// and assert_element commands.
//
// Placeholder: __AGENT_QUERY__ — JSON-encoded query object
//   { by: "target_id", value: "e5" }
//   { by: "text", value: "欢迎回来" }
//   { by: "selector", value: ".welcome-message" }

pub const WAIT_FOR_ELEMENT_SCRIPT: &str = r#"
(() => {
    const query = __AGENT_QUERY__;
    const timeout = __AGENT_TIMEOUT__ || 10000;
    const pollInterval = 100;

    function findElement() {
        if (query.by === 'target_id') {
            const id = CSS.escape(query.value);
            const el = document.querySelector(`[data-agent-id="${id}"]`);
            if (el) return { found: true, target_id: query.value };
            return { found: false };
        }

        if (query.by === 'text') {
            const els = document.querySelectorAll('[data-agent-id]');
            const target = query.value.toLowerCase();
            for (const el of els) {
                const text = (el.textContent || '').toLowerCase().trim();
                if (text.includes(target)) {
                    return { found: true, target_id: el.getAttribute('data-agent-id') };
                }
            }
            // 也检查 aria-label, placeholder, alt
            for (const el of els) {
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                const alt = (el.getAttribute('alt') || '').toLowerCase();
                if (aria.includes(target) || placeholder.includes(target) || alt.includes(target)) {
                    return { found: true, target_id: el.getAttribute('data-agent-id') };
                }
            }
            return { found: false };
        }

        if (query.by === 'selector') {
            const el = document.querySelector(query.value);
            if (el) {
                // 如果已有 data-agent-id，直接返回
                let id = el.getAttribute('data-agent-id');
                if (id) return { found: true, target_id: id };
                // 否则标记并返回
                const all = document.querySelectorAll('[data-agent-id]');
                id = 'e' + (all.length + 1);
                el.setAttribute('data-agent-id', id);
                return { found: true, target_id: id };
            }
            return { found: false };
        }

        return { found: false, error: 'unknown query type: ' + query.by };
    }

    // 先快速检查一次
    const fast = findElement();
    if (fast.found) return fast;

    // 轮询
    return new Promise((resolve) => {
        const start = Date.now();
        const timer = setInterval(() => {
            const result = findElement();
            if (result.found) {
                clearInterval(timer);
                resolve(result);
                return;
            }
            if (Date.now() - start > timeout) {
                clearInterval(timer);
                resolve({ found: false, timeout: true, query });
            }
        }, pollInterval);
    });
})();
"#;

// ═══════════════════════════════════════════════════════════════════════
// Assert Element Script — check if an element contains expected text
// ═══════════════════════════════════════════════════════════════════════
//
// Checks if the element identified by target_id contains the expected
// text. Returns { passed: bool, actual: string, target_id: string }.

pub const ASSERT_ELEMENT_SCRIPT: &str = r#"
(() => {
    const targetId = '__AGENT_TARGET_ID__';
    const expected = '__AGENT_EXPECTED_TEXT__';
    const id = CSS.escape(targetId);
    const el = document.querySelector(`[data-agent-id="${id}"]`);
    if (!el) {
        return { passed: false, error: 'Element not found', target_id: targetId };
    }
    const actual = (el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('alt') || '').trim();
    const passed = actual.toLowerCase().includes(expected.toLowerCase());
    return { passed, actual: actual.substring(0, 200), target_id: targetId };
})();
"#;

// ═══════════════════════════════════════════════════════════════════════
// Anti-Fingerprint Script — stolen from fingerprint browser techniques
// ═══════════════════════════════════════════════════════════════════════
//
// Injected via Page.addScriptToEvaluateOnNewDocument so it runs BEFORE
// any page JS. This script:
//   1. Canvas fingerprint noise (1% pixel noise in toDataURL/toBlob)
//   2. WebGL vendor/renderer spoofing (ANGLE/Intel)
//   3. navigator.plugins filling (headless Chrome has empty plugins)
//   4. navigator.languages / hardwareConcurrency / deviceMemory fixing
//   5. Screen colorDepth fixing
//   6. AudioContext fingerprint noise (subtle)
//   7. WebRTC protection (prevent real IP leak via iframe)
//   8. Chrome runtime info consistency (chrome.runtime, chrome.loadTimes)
//
// Reference: Multilogin / AdsPower / GoLogin anti-detection techniques.

pub const ANTI_FINGERPRINT_SCRIPT: &str = r#"
(() => {
    if (window.__agentAntiFingerprintInjected) return;
    window.__agentAntiFingerprintInjected = true;

    // ====== 1. Canvas 指纹噪声 ======
    // 在 toDataURL / toBlob / getImageData 上施加微量像素噪声，
    // 使每次渲染产生略有不同的指纹，防止网站通过 Canvas 指纹追踪。
    // 噪声幅度为 ±1（RGB 各通道），仅影响约 1% 的像素。
    {
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(...args) {
            const canvas = this;
            const ctx = canvas.getContext('2d');
            if (ctx) {
                try {
                    const w = canvas.width, h = canvas.height;
                    if (w > 0 && h > 0 && w * h < 10000000) {
                        const imageData = ctx.getImageData(0, 0, w, h);
                        const d = imageData.data;
                        // 对 ~1% 的像素施加 ±1 噪声
                        const noiseCount = Math.max(1, Math.floor((w * h) / 100));
                        for (let i = 0; i < noiseCount; i++) {
                            const idx = Math.floor(Math.random() * d.length / 4) * 4;
                            if (idx + 3 < d.length) {
                                d[idx]     = Math.min(255, Math.max(0, d[idx]     + (Math.random() < 0.5 ? 1 : -1)));
                                d[idx + 1] = Math.min(255, Math.max(0, d[idx + 1] + (Math.random() < 0.5 ? 1 : -1)));
                                d[idx + 2] = Math.min(255, Math.max(0, d[idx + 2] + (Math.random() < 0.5 ? 1 : -1)));
                            }
                        }
                        ctx.putImageData(imageData, 0, 0);
                    }
                } catch (_) {}
            }
            return origToDataURL.apply(this, args);
        };

        const origToBlob = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function(...args) {
            // 同样的噪声逻辑，通过 toDataURL 绕一圈
            const dataUrl = HTMLCanvasElement.prototype.toDataURL.apply(this, []);
            return origToBlob.apply(this, args);
        };
    }

    // ====== 2. WebGL 厂商/渲染器伪造 ======
    // 覆盖 WebGLRenderingContext.getParameter 返回伪造的 GPU 信息。
    // 使得 WebGL 指纹看起来像一台普通的 Intel 集成显卡设备。
    {
        const origGetParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {
            // UNMASKED_VENDOR_WEBGL (0x9245)
            if (param === 0x9245) return 'Intel Inc.';
            // UNMASKED_RENDERER_WEBGL (0x9246)
            if (param === 0x9246) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
            // VENDOR (0x1F00)
            if (param === 0x1F00) return 'WebKit (Intel Inc.)';
            // RENDERER (0x1F01)
            if (param === 0x1F01) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
            // VERSION (0x1F02) — 保持 WebGL 版本
            // SHADING_LANGUAGE_VERSION (0x8B8C) — 保持
            return origGetParameter.call(this, param);
        };
        // 也对 WebGL2 做同样的覆盖
        if (WebGL2RenderingContext) {
            const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {
                if (param === 0x9245) return 'Intel Inc.';
                if (param === 0x9246) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
                if (param === 0x1F00) return 'WebKit (Intel Inc.)';
                if (param === 0x1F01) return 'ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)';
                return origGetParameter2.call(this, param);
            };
        }
    }

    // ====== 3. navigator.plugins 填充 ======
    // headless Chrome 的 navigator.plugins 是空的（length=0），
    // 真实 Chrome 有 3-5 个插件。填充常见插件列表。
    {
        class FakePlugin {
            constructor(name, filename, desc, suffixes) {
                this.name = name;
                this.filename = filename;
                this.description = desc;
                this.length = 0;
                this[0] = { type: 'application/x-ppapi', suffixes: suffixes || '', description: desc ? desc.split(' ')[0] : '' };
            }
            item() { return null; }
            namedItem() { return null; }
        }
        const plugins = [
            new FakePlugin('Chrome PDF Plugin', 'internal-pdf-viewer', 'Portable Document Format', 'pdf'),
            new FakePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai', '', 'pdf'),
            new FakePlugin('Native Client', 'internal-nacl-plugin', '', ''),
        ];
        // 让 plugins 看起来像 PluginArray
        plugins.__proto__ = PluginArray.prototype;
        // 但是 Object.defineProperty 的 getter 不能有 __proto__ 赋值
        // 改用更简单的方式：直接覆盖
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = plugins;
                arr.length = 3;
                arr.item = (i) => arr[i] || null;
                arr.namedItem = (name) => arr.find(p => p.name === name) || null;
                arr.refresh = () => {};
                return arr;
            }
        });

        // 同样填充 mimeTypes
        const mimeTypes = [
            { type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: plugins[1] },
            { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: plugins[1] },
        ];
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => {
                const arr = mimeTypes;
                arr.length = 2;
                arr.item = (i) => arr[i] || null;
                arr.namedItem = (type) => arr.find(m => m.type === type) || null;
                return arr;
            }
        });
    }

    // ====== 4. 固定 navigator 属性 ======
    // 这些属性在 headless 模式下可能缺失或异常，统一设为常见值。
    {
        // 语言
        Object.defineProperty(navigator, 'languages', {
            get: () => ['zh-CN', 'zh', 'en']
        });
        // 硬件并发数（真实 Windows 设备通常为 4 或 8）
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8
        });
        // 设备内存（GB），真实设备通常为 4 或 8
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8
        });
        // 最大触控点数（0 表示非触控设备）
        Object.defineProperty(navigator, 'maxTouchPoints', {
            get: () => 0
        });
    }

    // ====== 5. 屏幕属性固定 ======
    // 确保屏幕属性与普通 Windows 设备一致
    {
        Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
        Object.defineProperty(screen, 'pixelDepth', { get: () => 24 });
        Object.defineProperty(screen, 'availWidth', { get: () => 1280 });
        Object.defineProperty(screen, 'availHeight', { get: () => 720 });
        Object.defineProperty(screen, 'width', { get: () => 1280 });
        Object.defineProperty(screen, 'height', { get: () => 720 });
    }

    // ====== 6. WebRTC 保护 ======
    // 防止 WebRTC 泄露真实内网 IP
    // 覆盖 RTCPeerConnection 的 createOffer/createAnswer
    {
        const origCreateOffer = RTCPeerConnection.prototype.createOffer;
        RTCPeerConnection.prototype.createOffer = function(...args) {
            // 设置 ICE 候选策略为 relay-only（仅中继，不暴露 IP）
            if (this.iceTransportPolicy === undefined) {
                try { this.iceTransportPolicy = 'relay'; } catch (_) {}
            }
            return origCreateOffer.apply(this, args);
        };
    }

    // ====== 7. chrome.runtime 一致性 ======
    // 某些网站检查 chrome.runtime 是否存在来判断是否为真实浏览器
    if (window.chrome && chrome.runtime) {
        // 保持原样 — 真实 Chrome 有 runtime
    }

    // ====== 8. 权限查询保护 ======
    // 某些网站通过 navigator.permissions.query 检测 headless 特征
    {
        const origQuery = navigator.permissions.query;
        navigator.permissions.query = function(desc) {
            if (desc && desc.name === 'camera') {
                return Promise.resolve({ state: 'prompt', onchange: null });
            }
            if (desc && desc.name === 'microphone') {
                return Promise.resolve({ state: 'prompt', onchange: null });
            }
            if (desc && desc.name === 'notifications') {
                return Promise.resolve({ state: 'prompt', onchange: null });
            }
            return origQuery.call(this, desc);
        };
    }
})();
"#;

// M-02 fix: WAIT_FOR_IDLE_SCRIPT removed.
// Network idle detection now uses CDP Network domain events in Rust,
// which cannot be spoofed by page JavaScript. See browser.rs NetworkIdleTracker.
