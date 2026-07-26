# Workbuddy — MCP 配置

[Workbuddy](https://github.com/kingzcheung/workbuddy) 是一款 AI 编程助手，
支持 MCP 协议集成外部工具。

## 配置方式

将 MCP Server 配置添加到 Workbuddy 的 `mcp_settings.json` 或对应平台设置中：

### mcp_settings.json

```json
{
  "mcpServers": {
    "agent-browser": {
      "command": "python",
      "args": ["D:/agent_browser_cli/ai_browser_cli_repo/mcp_server.py"],
      "disabled": false,
      "autoApprove": [
        "navigate",
        "page_tree",
        "page_meta",
        "screenshot",
        "get_prompt"
      ]
    }
  }
}
```

> **提示**：`autoApprove` 中列出的只读操作（navigate、page_tree、page_meta、screenshot）建议设为自动批准；
> `click` 和 `type_text` 涉及页面交互，建议手动确认。

### VS Code 集成

如果 Workbuddy 是 VS Code 插件，在 `settings.json` 中配置：

```json
{
  "workbuddy.mcpServers": {
    "agent-browser": {
      "command": "python",
      "args": ["D:/agent_browser_cli/ai_browser_cli_repo/mcp_server.py"]
    }
  }
}
```

## 可用工具

| 工具 | 写入操作 | 说明 |
|------|----------|------|
| `navigate` | 否 | 打开网页 |
| `click` | **是** | 点击元素（建议手动确认） |
| `type_text` | **是** | 输入文字（建议手动确认） |
| `screenshot` | 否 | 页面截图 |
| `page_tree` | 否 | 获取页面交互树 |
| `page_meta` | 否 | 页面元数据 |
| `get_prompt` | 否 | 获取 Agent 提示词 |

## Workbuddy 中使用示例

在对话中直接使用：

```
@agent-browser navigate https://www.github.com
@agent-browser page_tree
```
