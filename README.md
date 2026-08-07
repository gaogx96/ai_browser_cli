# Agent Browser CLI

AI Agent 专属浏览器 CLI — 通过 CDP 协议操控真实 Chrome，为 LLM 提供网页感知与操作能力。

## 核心能力

- **接管已登录的 Chrome** — `--connect` 模式直连人类已开启的浏览器，100% 保留登录态
- **扩展模式（无感）** — 通过 Chrome 扩展 + `chrome.debugger` API 操控浏览器，无需 `--remote-debugging-port`
- **自动检测** — 50ms 探测 9222 端口，有 Chrome 就接管，没有就自动拉起强化无头实例
- **跨 iframe 滚动** — 自动穿透多层 iframe，跨域时降级为 CDP 父页面滚动
- **LoaderId 隔离空闲检测** — 每次导航独立计数器，旧页面事件不会污染新页面
- **多维分析过滤** — 50+ 广告/追踪域名 + 路径关键词 + 资源类型 + beacon 文件，确保秒级空闲判定
- **反检测** — 禁用 `navigator.webdriver`、真实 User-Agent、1280×720 视口
- **指纹浏览器级反检测** — Canvas 噪声、WebGL 伪造、plugins 填充、WebRTC 保护等 8 大防护
- **拟人化输入** — CJK 用 `InputEvent`，ASCII 用 `KeyboardEvent`，逐键随机延迟
- **正文提取** — Readability 风格算法，自动识别页面正文，去除导航/广告噪声
- **等待元素** — 按文本/选择器/ID 轮询等待动态元素出现
- **内容断言** — 验证元素是否包含预期文本，支持 UI 自动化测试
- **文件下载** — 端口模式用 CDP 下载管理，扩展模式用 `chrome.downloads` API
- **Windows Job Object** — 进程异常退出时 Chrome 自动被内核级机制终止
- **内置 Agent Prompt** — `prompt` 子命令直接输出 LLM 系统提示词
- **Agent 自主规划** — `run_task` 让 Agent 自主观察→决策→执行→观察，完成多步浏览器任务
- **不抢标签页** — 扩展模式 `active: false` 后台创建标签页，兜底恢复焦点，全程无感

## 快速开始

```powershell
# 构建
cargo build --release

# 方式 1: 扩展模式（推荐，无感）
# 需要在 Chrome 中加载 extension/ 目录（unpacked）
.\target\release\agent-browser-cli.exe view --url "https://www.baidu.com" --extension

# 方式 2: 自动检测
# 如果 9222 端口有 Chrome → 自动接管（保留登录态）
# 如果没有 → 拉起新的无头 Chrome
.\target\release\agent-browser-cli.exe view --url "https://www.baidu.com"

# 方式 3: 显式连接已登录的 Chrome
# 先启动: chrome.exe --remote-debugging-port=9222
.\target\release\agent-browser-cli.exe view --connect http://127.0.0.1:9222 --url "https://github.com"

# 方式 4: 管道监听模式（SDK 集成用）
.\target\release\agent-browser-cli.exe listen --extension
```

## 子命令

### `view` — 单次页面抓取

```powershell
agent-browser-cli view --url <URL> [--connect <URL>] [--profile <PATH>] [--show]
```

### `listen` — 管道监听模式

```powershell
agent-browser-cli listen [--connect <URL>] [--profile <PATH>] [--resources block|allow|smart] [--show] [--extension]
```

资源策略：
- `block` — 阻断图片/CSS/字体/广告（最快，默认）
- `allow` — 允许所有资源（完整渲染）
- `smart` — 只阻断广告/追踪，允许图片和 CSS

### `prompt` — 输出 Agent 系统提示词

```powershell
agent-browser-cli prompt
```

直接将内置的 LLM 系统提示词输出到 stdout，可管道传给 AI 框架。

## Agent 自主规划（新增）

给 Agent 一个自然语言任务，它能自主分解为多步浏览器操作并执行。

### 通过 MCP 工具

```python
# 在 Claude Code 或其他 MCP 客户端中调用
run_task("打开 https://example.com 并告诉我页面标题")
run_task("在百度搜索 Rust 并点击第一条结果")
run_task("下载这个钉钉文档到桌面")
```

### 通过 Python 独立运行

```bash
cd /d/agent_browser_cli/ai_browser_cli_repo
python agent_runner.py "打开 https://example.com 并读取标题"
```

### 工作原理

Agent 采用 **ReAct 循环**（观察→决策→执行→观察）：

1. **观察** — 调用 `page_tree` / `meta` 获取当前页面状态
2. **决策** — 把任务 + 页面树 + 历史发给 LLM，LLM 返回下一步动作
3. **执行** — 执行 LLM 返回的动作（navigate/click/type/evaluate/download_setup）
4. **记录** — 动作结果入历史，供下一步参考
5. **循环** — 直到 LLM 输出 `stop` 或达到最大步数

### 支持的 LLM 后端

| 后端 | 环境变量 |
|------|---------|
| Anthropic（默认） | `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` |
| OpenAI | `OPENAI_API_KEY` |

### 动作列表

| 动作 | 说明 |
|------|------|
| `navigate` | 打开 URL |
| `click` | 点击元素（`target_id`） |
| `type` | 输入文本 |
| `evaluate` | 执行任意 JavaScript（绕过 50 字符截断） |
| `download_setup` | 设置下载目录 |
| `stop` | 任务完成或遇到障碍时终止 |

### 错误恢复

- 同一元素失败 3 次自动加入黑名单，换策略
- 链接被截断时自动用 `evaluate` 获取完整 URL
- 遇到验证码/登录墙时自动终止并提示用户

### 运行要求

```bash
# 环境变量
export AGENT_BROWSER_EXTENSION=1  # 必须
export LLM_PROVIDER=anthropic      # 或 openai
export ANTHROPIC_API_KEY=sk-xxx   # 你的 API key
export AGENT_MAX_STEPS=15          # 可选，默认 15 步
```

## 不抢标签页（新增）

Agent 全程在后台运行，不影响用户当前浏览：

### 三层防护

1. **扩展端 `active: false`** — 所有新标签页在后台创建，不抢焦点
2. **Rust 侧兜底恢复** — connect 后若标签页意外抢到焦点，立即恢复原活动页
3. **`detect_new_tab` 不激活** — 发现新标签页只更新内部引用，不激活

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_BROWSER_BACKGROUND` | `1` | 后台创建标签页 |
| `AGENT_BROWSER_RESTORE_FOCUS` | `1` | 启用兜底恢复焦点（设为 `0` 禁用） |

## 扩展模式

通过 Chrome 扩展 + `chrome.debugger` API 操控浏览器，无需 `--remote-debugging-port`。

### 前置条件

1. 在 Chrome 中加载扩展：打开 `chrome://extensions` → 开启开发者模式 → 加载已解压的扩展程序 → 选择 `extension/` 目录
2. 确保当前活动标签页是普通网页（非 `chrome://` 页面）
3. 运行命令时加 `--extension` 参数

### 用法

```powershell
# 一次性提取
.\target\release\agent-browser-cli.exe view --url "https://example.com" --extension

# 常驻管道模式
.\target\release\agent-browser-cli.exe listen --extension
```

### 能力边界

| 功能 | 端口模式 | 扩展模式 |
|---|---|---|
| navigate / click / type | ✅ | ✅ |
| extract_tree（含跨域 iframe） | ✅ | ✅ |
| 正文提取 | ✅ | ✅ |
| 等待元素 / 断言 | ✅ | ✅ |
| 文件下载 | ✅ | ✅（通过 `chrome.downloads` API） |
| 截图 | ✅ | 仅前台 tab 可用，后台 tab 受 `chrome.debugger` 限制 |
| 退出安全 | ✅ | ✅ 不关用户 tab，不 kill 浏览器 |
| evaluate（任意 JS） | ✅ | ✅ |
| Agent 自主规划（run_task） | ✅ | ✅ |
| 后台运行不抢焦点 | ✅ | ✅ |

### 扩展模式截图限制

`chrome.debugger` 的 `Page.captureScreenshot` 要求 tab 为前台可见状态。后台 tab 截图会超时或失败。
如需截图，请切换到端口模式（`--connect`）或确保 agent tab 在前台。

### 回归测试

```powershell
# 运行回归测试（需要 fixture 服务器 + Chrome 扩展已加载）
.\tests\regression.ps1
```

### 测试脚本

```powershell
# 回归测试（自动，三模式 extract 一致性 + 下载）
.\tests\regression.ps1

# 动态插帧测试（自动，验证 click 不点偏）
.\tests\dynamic_frame_test.ps1

# Tab 存活测试（半自动，需确认 Chrome 有 3+ 标签页）
.\tests\tab_safety_test.ps1
```

资源策略：
- `block` — 阻断图片/CSS/字体/广告（最快，默认）
- `allow` — 允许所有资源（完整渲染）
- `smart` — 只阻断广告/追踪，允许图片和 CSS

### `prompt` — 输出 Agent 系统提示词

```powershell
agent-browser-cli prompt
```

直接将内置的 LLM 系统提示词输出到 stdout，可管道传给 AI 框架。

## JSON 管道协议

`listen` 模式下，通过 stdin 发送 JSON 命令，stdout 返回 JSON 响应：

```json
{"action": "navigate", "url": "https://example.com"}
{"action": "click", "target_id": "e5"}
{"action": "type", "target_id": "e3", "text": "hello world"}
{"action": "screenshot"}
{"action": "tree"}
{"action": "meta"}
{"action": "get_content"}
{"action": "wait_for", "by": "text", "value": "欢迎回来", "timeout": 10000}
{"action": "assert_element", "target_id": "e5", "expected": "登录成功"}
{"action": "download_setup", "path": "downloads"}
{"action": "download", "target_id": "e10", "path": "downloads", "timeout": 30000}
{"action": "evaluate", "expression": "document.title"}
{"action": "configure", "media_enabled": true}
{"action": "get_prompt"}
{"action": "run_task", "task": "打开 https://example.com 并读取标题", "max_steps": 15}
```

### 输出格式

```json
{"status": "ok", "action": "navigate", "url": "...", "title": "...", "interactive_count": 32, "tree": "[@e1] button \"登录\"\n[@e2] input \"手机号\""}
{"status": "error", "error": "Element [e5] not found in any frame"}
```

### `configure` 运行时切换

| `media_enabled` | 效果 |
|:---:|---|
| `true` | 清空拦截列表，允许加载所有资源 |
| `false` | 重新注入拦截黑名单 |

### `get_content` — 提取页面正文

```json
{"action": "get_content"}
{"status": "ok", "action": "get_content", "content": {"title": "...", "text": "...", "wordCount": 123, "charCount": 456, "method": "readability"}}
```

使用 Readability 风格算法自动识别页面正文区域，返回结构化文本。适用于：
- 提取文章/新闻正文
- 获取页面主要内容（去除导航/广告/侧栏噪声）
- 为 LLM 提供可读的页面文本（而非仅交互元素）

### `wait_for` — 等待元素出现

```json
{"action": "wait_for", "by": "text", "value": "欢迎回来", "timeout": 10000}
{"status": "ok", "action": "wait_for", "result": {"found": true, "target_id": "e5"}}
```

轮询等待指定元素出现，支持三种查询方式：
- `by: "text"` — 按文本内容匹配（大小写不敏感）
- `by: "target_id"` — 按 data-agent-id 匹配
- `by: "selector"` — 按 CSS 选择器匹配

适用于 SPA 页面等待动态内容渲染完成。

### `assert_element` — 断言元素内容

```json
{"action": "assert_element", "target_id": "e5", "expected": "登录成功"}
{"status": "ok", "action": "assert_element", "result": {"passed": true, "actual": "登录成功，欢迎回来", "target_id": "e5"}}
```
适用于 UI 自动化测试中的断言验证。

### `download_setup` + `download` — 文件下载

```json
// 1. 设置下载目录（可选，listen 启动后先配置一次）
{"action": "download_setup", "path": "downloads"}

// 2. 点击下载链接，等待文件下载完成
{"action": "download", "target_id": "e10", "path": "downloads", "timeout": 30000}
{"status": "ok", "action": "download", "result": {"status": "ok", "guid": "...", "download_path": "downloads"}}
```

`download_setup` 启用浏览器下载行为并设置下载路径。`download` 点击指定元素触发的下载，并等待下载完成（支持超时）。

## SDK

### Python

```python
from browser_client import BrowserClient
import asyncio

async def main():
    async with BrowserClient(connect="http://127.0.0.1:9222") as client:
        result = await client.navigate("https://example.com")
        print(result["tree"])
        await client.screenshot()

asyncio.run(main())
```

### TypeScript

```typescript
import { BrowserClient } from './browserClient';

const client = new BrowserClient({ connect: 'http://127.0.0.1:9222' });
await client.start();
const result = await client.navigate('https://example.com');
console.log(result.tree);
await client.close();
```

## 无障碍树格式

每个可交互元素输出一行：
```
[@eN] role "text_content"
```

- `eN` — 全局唯一 ID（跨 iframe 递增）
- `role` — 元素角色（button, link, input, textbox 等）
- `text_content` — 文本内容/placeholder/aria-label（截断到 50 字符）

多 iframe 场景带 frame 前缀：
```
[frame-0] [@e1] button "登录"
[frame-1] [@e50] input "搜索"
```

## 反检测配置

启动时自动注入（CDP 启动参数 + 页面 JS 注入）：

### CDP 启动参数层
- `--disable-blink-features=AutomationControlled` — 隐藏 `navigator.webdriver`
- `--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...` — 真实浏览器 UA
- `window_size(1280, 720)` — 标准 Windows 分辨率

### JS 注入层（指纹浏览器级反检测）
通过 `Page.addScriptToEvaluateOnNewDocument` 在所有页面加载前注入，覆盖 iframe 和新标签页：

| 保护项 | 措施 |
|:---|---:|
| Canvas 指纹 | 1% 像素 ±1 噪声 |
| WebGL 指纹 | 伪造 Intel UHD Graphics 620 渲染器 |
| `navigator.plugins` | 填充 PDF 插件、Native Client |
| `navigator.languages` | 固定 `['zh-CN', 'zh', 'en']` |
| `navigator.hardwareConcurrency` | 固定 8 核 |
| `navigator.deviceMemory` | 固定 8 GB |
| 屏幕属性 | 固定 24-bit colorDepth、1280×720 |
| WebRTC | ICE relay-only 策略，防 IP 泄露 |
| 权限查询 | 统一返回 `prompt` 状态 |

## 僵尸进程防护

三重机制确保 Chrome 不会残留：
1. **Stdin 断开自毁** — 管道关闭时自动退出
2. **Ctrl+C 信号处理** — `tokio::signal::ctrl_c()` 优雅关闭
3. **Windows Job Object** — 内核级 `KILL_ON_JOB_CLOSE`，进程崩溃时 OS 自动清理

## 手动验证测试

### 动态插帧测试

验证 frame 索引快照在页面动态变化后仍能正确寻址：

```powershell
# 1. 启动 fixture 服务器
python -m http.server 8080 --directory tests/fixtures &
python -m http.server 8081 --directory tests/fixtures &

# 2. 运行 extract
.\target\debug\agent-browser-cli.exe view --url "http://127.0.0.1:8080/oopif_main.html" --extension

# 3. 在浏览器控制台执行：
#    const f = document.createElement('iframe');
#    f.src = 'http://127.0.0.1:8081/oopif_frame.html';
#    document.body.insertBefore(f, document.body.firstChild);

# 4. 再次运行 extract，确认 f0-e1 仍是原元素
```

### 用户 tab 存活测试

验证 `view/listen --extension` 退出时不会误关用户标签页：

```powershell
# 1. 在 Chrome 中手动打开 3 个标签页（如 baidu / github / 本地页）
# 2. 运行：
.\target\debug\agent-browser-cli.exe view --url "https://example.com" --extension
# 3. 确认退出后 3 个标签页仍在
# 4. 运行：
.\target\debug\agent-browser-cli.exe listen --extension
# 5. 在 stdin 输入：{"action":"navigate","url":"https://example.com"}
# 6. Ctrl+C 退出，确认 3 个标签页仍在
```

## 扩展开发

### 打包扩展

```powershell
# 生成密钥对（用于固定扩展 ID）
openssl genrsa 2048 | openssl pkcs8 -topk8 -nocrypt > extension.pem
# 打包
chrome --pack-extension=extension --pack-extension-key=extension.pem
```

## License

MIT
