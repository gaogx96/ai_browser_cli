# Agent Browser CLI

AI Agent 专属浏览器 CLI — 通过 CDP 协议操控真实 Chrome，为 LLM 提供网页感知与操作能力。

## 核心能力

- **接管已登录的 Chrome** — `--connect` 模式直连人类已开启的浏览器，100% 保留登录态
- **自动检测** — 50ms 探测 9222 端口，有 Chrome 就接管，没有就自动拉起强化无头实例
- **跨 iframe 滚动** — 自动穿透多层 iframe，跨域时降级为 CDP 父页面滚动
- **LoaderId 隔离空闲检测** — 每次导航独立计数器，旧页面事件不会污染新页面
- **多维分析过滤** — 50+ 广告/追踪域名 + 路径关键词 + 资源类型 + beacon 文件，确保秒级空闲判定
- **反检测** — 禁用 `navigator.webdriver`、真实 User-Agent、1280×720 视口
- **拟人化输入** — CJK 用 `InputEvent`，ASCII 用 `KeyboardEvent`，逐键随机延迟
- **Windows Job Object** — 进程异常退出时 Chrome 自动被内核级机制终止
- **内置 Agent Prompt** — `prompt` 子命令直接输出 LLM 系统提示词

## 快速开始

```powershell
# 构建
cargo build --release

# 方式 1: 自动检测（推荐）
# 如果 9222 端口有 Chrome → 自动接管（保留登录态）
# 如果没有 → 拉起新的无头 Chrome
.\target\release\agent-browser-cli.exe view --url "https://www.baidu.com"

# 方式 2: 显式连接已登录的 Chrome
# 先启动: chrome.exe --remote-debugging-port=9222
.\target\release\agent-browser-cli.exe view --connect http://127.0.0.1:9222 --url "https://github.com"

# 方式 3: 管道监听模式（SDK 集成用）
.\target\release\agent-browser-cli.exe listen --connect http://127.0.0.1:9222
```

## 子命令

### `view` — 单次页面抓取

```powershell
agent-browser-cli view --url <URL> [--connect <URL>] [--profile <PATH>] [--show]
```

### `listen` — 管道监听模式

```powershell
agent-browser-cli listen [--connect <URL>] [--profile <PATH>] [--resources block|allow|smart] [--show]
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
{"action": "configure", "media_enabled": true}
{"action": "get_prompt"}
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

启动时自动注入：
- `--disable-blink-features=AutomationControlled` — 隐藏 `navigator.webdriver`
- `--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...` — 真实浏览器 UA
- `window_size(1280, 720)` — 标准 Windows 分辨率

## 僵尸进程防护

三重机制确保 Chrome 不会残留：
1. **Stdin 断开自毁** — 管道关闭时自动退出
2. **Ctrl+C 信号处理** — `tokio::signal::ctrl_c()` 优雅关闭
3. **Windows Job Object** — 内核级 `KILL_ON_JOB_CLOSE`，进程崩溃时 OS 自动清理

## License

MIT
