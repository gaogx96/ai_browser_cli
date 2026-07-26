# Claude Code — MCP 配置

将以下配置放入项目根目录下的 `.claude/settings.local.json` 中：

```json
{
  "mcpServers": {
    "agent-browser": {
      "command": "python",
      "args": ["mcp_server.py"]
    }
  }
}
```

## 使用说明

1. **构建二进制**：
   ```powershell
   cd <项目目录>
   cargo build --release
   ```

2. **安装依赖**：
   ```powershell
   pip install mcp
   ```

3. **在 Claude Code 中使用**：
   ```bash
   # 导航到页面
   /agent-browser navigate url="https://www.example.com"
   
   # 点击元素
   /agent-browser click target_id="e5"
   
   # 输入文本
   /agent-browser type_text target_id="e3" text="搜索内容"
   
   # 截图
   /agent-browser screenshot
   
   # 获取页面树
   /agent-browser page_tree
   ```

> **注意**：`settings.local.json` 是本地覆盖文件，不会提交到 Git。
> 如果项目中已有 `.claude/settings.json`，也可以将配置合并到其中。
