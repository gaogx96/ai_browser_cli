# Open Claw — MCP/Tool 配置

[Open Claw](https://github.com/nicepkg/open-claw) 是一个浏览器自动化工具，
支持通过 MCP 集成外部能力。

## 配置方式

在 Open Claw 的配置文件中添加 MCP Server：

### config.json

```json
{
  "mcpServers": [
    {
      "name": "agent-browser",
      "command": "python",
      "args": ["path/to/mcp_server.py"],
      "tools": [
        "navigate",
        "click",
        "type_text", 
        "screenshot",
        "page_tree",
        "page_meta"
      ]
    }
  ]
}
```

### 或通过环境变量

```bash
OPEN_CLAW_MCP_SERVERS='[{"name":"agent-browser","command":"python","args":["mcp_server.py"]}]'
```

## 集成说明

Open Claw 内置了浏览器控制能力，agent-browser-cli 可作为**增强选项**：
- 提供更高效的无障碍树提取（比 DOM 解析更快）
- 内置反检测机制（隐藏 `navigator.webdriver`）
- 自动 iframe 穿透和 ID 标记
- 支持连接已登录 Chrome 绕过验证码

## 工作流示例

```json
{
  "steps": [
    {
      "tool": "agent-browser.navigate",
      "args": { "url": "https://www.baidu.com" }
    },
    {
      "tool": "agent-browser.type_text",
      "args": { "target_id": "e0", "text": "天气" }
    },
    {
      "tool": "agent-browser.click",
      "args": { "target_id": "e1" }
    },
    {
      "tool": "agent-browser.screenshot",
      "args": {}
    }
  ]
}
```
