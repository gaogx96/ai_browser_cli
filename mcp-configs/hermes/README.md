# Hermes — MCP/Tool 配置

[Hermes](https://github.com/arcprize/hermes) 支持通过 MCP 协议集成外部工具。

## 配置方式

在 Hermes 的 `config.yaml` 或环境变量中添加 MCP Server：

### 方式 1：配置文件

```yaml
# ~/.config/hermes/config.yaml 或项目 hermes-config.yaml
mcp_servers:
  agent-browser:
    command: python
    args:
      - mcp_server.py
    env:
      # 如果需要自定义 Chrome 连接
      # AGENT_BROWSER_CONNECT: "http://127.0.0.1:9222"
```

### 方式 2：环境变量

```bash
export HERMES_MCP_SERVERS='{"agent-browser": {"command": "python", "args": ["mcp_server.py"]}}'
```

## 可用工具

| 工具名 | 参数 | 说明 |
|--------|------|------|
| `navigate` | `url` (必填) | 打开 URL，返回页面交互树 |
| `click` | `target_id` (必填) | 点击指定 ID 的元素 |
| `type_text` | `target_id`, `text` (必填) | 在输入框中输入文字 |
| `screenshot` | 无 | 截取当前页面截图 |
| `page_tree` | 无 | 获取页面无障碍交互树 |
| `page_meta` | 无 | 获取页面元数据 |
| `get_prompt` | 无 | 获取内置 AI Agent 提示词 |

## 使用 Hermes Python SDK

```python
from hermes import Agent

agent = Agent(tools=["agent-browser"])
result = await agent.run("打开百度搜索 Claude")
```

## 使用 Hermes CLI

```bash
hermes run "打开百度，搜索今天天气" --tool agent-browser
```
