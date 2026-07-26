# MCP Configurations for Agent Browser CLI

本项目为 **agent-browser-cli** 提供了完整的 MCP (Model Context Protocol) 配置，
使其可作为浏览器自动化工具被 AI Agent 框架调用。

## 构建

```powershell
cd agent_browser_cli_repo
cargo build --release
```

构建后的二进制文件位于：`target/release/agent-browser-cli.exe`

## MCP Server 桥接层

`mcp_server.py` 是一个 Python MCP Server，它将 agent-browser-cli 的 JSON 管道协议
转换为标准的 MCP JSON-RPC 协议，暴露了以下工具：

| MCP 工具 | 功能 |
|----------|------|
| `navigate` | 导航到指定 URL，返回页面无障碍树 |
| `click` | 按元素 ID 点击（如 `e5`） |
| `type_text` | 向输入框输入文本（拟人化延迟） |
| `screenshot` | 截取当前页面截图 |
| `page_tree` | 提取当前页面的无障碍交互树 |
| `page_meta` | 获取页面元信息（标题、URL、交互元素数） |
| `get_prompt` | 获取内置 AI Agent 系统提示词 |

启动方式：
```powershell
cd agent_browser_cli_repo
python mcp_server.py
```

详情见各子目录下的独立配置文件。
