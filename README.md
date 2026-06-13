# Agent Browser CLI

AI Agent 专属浏览器 CLI 方案 — Windows 专用版

## 项目结构

```
agent-browser-cli/
├── Cargo.toml                    # Rust 依赖与 CLI 元数据
├── src/
│   ├── main.rs                   # 命令行解析与命令分发 (clap)
│   ├── browser.rs                # Chromium 核心控制与网络拦截 (CDP)
│   ├── injector.rs               # JS 运行时补丁 (A11y 提取、智能滚动、DOM 绑定)
│   └── utils.rs                  # 截图调试与本地文件 IO
└── sdk/
    ├── python/
    │   └── agent_browser_client.py   # Python 异步客户端
    └── typescript/
        ├── src/index.ts              # TypeScript/Node.js 客户端
        ├── package.json
        └── tsconfig.json
```

## 前置要求

1. **Rust 工具链** (安装 [rustup](https://rustup.rs/))
   ```powershell
   winget install Rustlang.Rustup
   # 或者访问 https://rustup.rs 下载安装
   ```

2. **Chrome 或 Edge 浏览器** (已安装在 Windows 上)

## 构建

```powershell
# 在项目根目录执行
cargo build --release

# 生成的可执行文件位于
# .\target\release\agent-browser-cli.exe
```

## 使用方式

### 1. 单次页面抓取 (`view`)

```powershell
.\target\release\agent-browser-cli.exe view --url "https://ycombinator.com"

# 使用 Chrome 登录态
.\target\release\agent-browser-cli.exe view --url "https://github.com" --profile "C:\Users\You\AppData\Local\Google\Chrome\User Data"
```

### 2. 管道监听模式 (`listen`)

```powershell
# 启动监听模式（默认激进拦截图片/CSS/字体）
.\target\release\agent-browser-cli.exe listen

# 启动时就允许加载媒体资源（跳过初始拦截）
.\target\release\agent-browser-cli.exe listen --media-enabled
```

发送 JSON 命令到 stdin：

```json
{"action": "navigate", "url": "https://example.com"}
{"action": "click", "target_id": "e5"}
{"action": "type", "target_id": "e3", "text": "hello world"}
{"action": "screenshot"}
{"action": "tree"}
{"action": "meta"}
{"action": "configure", "media_enabled": true}
```

#### `configure` 动态切换说明

| `media_enabled` | 效果 |
|:---:|---|
| `true` | 清空拦截列表，允许加载所有图片、视频、CSS |
| `false` | 重新注入激进拦截黑名单（`*.css`, `*.png`, `*.jpg`, `*.mp4` 等） |

切换后自动等待 100ms 让 Chromium 重置网络状态，避免 Windows 下引擎卡死。

### 3. Python SDK

```python
import asyncio
from agent_browser_client import AgentBrowserClient

async def main():
    async with AgentBrowserClient() as client:
        await client.navigate("https://example.com")
        tree = await client.get_tree()
        print(tree)
        await client.screenshot()

asyncio.run(main())
```

### 4. TypeScript SDK

```typescript
import { AgentBrowserClient } from 'agent-browser-client';

const client = new AgentBrowserClient();
await client.start();
await client.navigate('https://example.com');
const tree = await client.getTree();
console.log(tree);
await client.stop();
```

## 功能特性

- **激进网络拦截**: 自动阻断 CSS、图片、字体、广告资源，极限加速页面加载
- **动态媒体切换**: 运行时通过 `configure` 指令实时开关媒体拦截，无需重启
- **网络空闲等待**: 导航和点击后自动等待 networkIdle 状态，杜绝返回 Skeleton 半成品页面
- **智能平滑滚动**: 交互前自动检测元素可见性，避开 Sticky Header 遮挡
- **无障碍树提取**: 为每个可交互元素生成 `[@eN] role "text"` 格式的 AI 友好文本
- **新标签页追踪**: 点击链接弹出新窗口时自动切换焦点，后续操作无缝衔接
- **拟人化输入**: 输入字符逐键事件派发 + 30ms±15ms 随机延迟，绕过自动化风控
- **僵尸进程三重防护**: Stdin 断开自毁 + Ctrl+C 信号处理 + Windows Job Object 内核级兜底
- **Windows 兼容**: 正确处理路径分隔符、.exe 后缀、CREATE_NO_WINDOW 标志

## 命令行参数

```
agent-browser-cli <COMMAND>

Commands:
  view      One-shot page load: navigate, extract AI tree, then exit
  listen    Persistent pipe mode: listen on stdin for JSON commands

Options:
  -h, --help     Print help
  -V, --version  Print version
```

### view 子命令

```
agent-browser-cli view --url <URL> [--profile <PATH>]

Options:
  -u, --url <URL>          URL to load
  -p, --profile <PATH>     Chrome profile path for session reuse
```

### listen 子命令

```
agent-browser-cli listen [--profile <PATH>] [--block-resources <BOOL>] [--media-enabled]

Options:
  -p, --profile <PATH>              Chrome profile path for session reuse
  -b, --block-resources <BOOL>      Enable aggressive resource blocking [default: true]
      --media-enabled               Start with media loading enabled (skip initial blocking)
```

## 输出格式

### 成功响应

```json
{
  "status": "ok",
  "action": "navigate",
  "url": "https://example.com",
  "title": "Example Domain",
  "interactive_count": 1,
  "tree": "[@e1] link \"More information...\""
}
```

### configure 响应

```json
{
  "status": "ok",
  "action": "configure",
  "media_enabled": true
}
```

### 错误响应

```json
{
  "status": "error",
  "error": "Element [e99] not found or not clickable"
}
```

## 僵尸进程防护验证

1. 启动 Python/TS 脚本调用 `listen` 模式
2. 在任务管理器中强制关闭 Python/TS 进程
3. 观察 Chrome.exe 是否随之自动销毁

## License

MIT
