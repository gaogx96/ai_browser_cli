"""
Agent Browser CLI — MCP Server Bridge

Translates between MCP (Model Context Protocol) JSON-RPC and
agent-browser-cli's JSON pipe protocol, exposing browser automation
as standard MCP tools for Claude Code, Hermes, Open Claw, Workbuddy.

Usage:
    python mcp_server.py
    # Starts in MCP stdio mode — configure in settings.json as:
    # "mcpServers": { "agent-browser": { "command": "python", "args": ["mcp_server.py"] } }
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)

# ── Constants ──────────────────────────────────────────────────────────

BINARY_NAME = "agent-browser-cli.exe"
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_BINARY = str(PROJECT_ROOT / "target" / "release" / BINARY_NAME)
CONNECT_TIMEOUT = 30  # seconds to wait for the "ready" signal
COMMAND_TIMEOUT = 120  # seconds per command

# ── Subprocess manager ─────────────────────────────────────────────────


class BrowserSubprocess:
    """Manages the agent-browser-cli subprocess lifecycle."""

    def __init__(self, binary: str = DEFAULT_BINARY) -> None:
        self._binary = binary
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._proc is not None:
            return

        if not os.path.isfile(self._binary):
            raise RuntimeError(
                f"agent-browser-cli binary not found at: {self._binary}\n"
                f"Run: cd {PROJECT_ROOT} && cargo build --release"
            )

        cmd = [self._binary, "listen"]

        # 资源策略：可通过环境变量 AGENT_BROWSER_RESOURCES 配置
        # block=阻断所有(最快), allow=允许所有, smart=只阻断广告
        resources = os.environ.get("AGENT_BROWSER_RESOURCES", "smart")
        if resources in ("block", "allow", "smart"):
            cmd.extend(["--resources", resources])

        # 扩展模式（推荐）：通过 Chrome 扩展 + chrome.debugger API 连接，无需 --remote-debugging-port
        # 用法: env AGENT_BROWSER_EXTENSION=1
        use_extension = os.environ.get("AGENT_BROWSER_EXTENSION", "")
        if use_extension and use_extension.lower() in ("1", "true", "yes"):
            cmd.append("--extension")

        # 支持通过环境变量连接已有 Chrome 实例（需 --remote-debugging-port）
        # 用法: env AGENT_BROWSER_CONNECT=http://127.0.0.1:9222
        connect_url = os.environ.get("AGENT_BROWSER_CONNECT", "")
        if connect_url and not use_extension:
            cmd.extend(["--connect", connect_url])

        profile = os.environ.get("AGENT_BROWSER_PROFILE", "")
        if profile:
            cmd.extend(["--profile", profile])

        show = os.environ.get("AGENT_BROWSER_SHOW", "")
        if show and show.lower() in ("1", "true", "yes"):
            cmd.append("--show")

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        # Drain stderr in background
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Wait for "ready" signal
        ready = await self._read_line(timeout=CONNECT_TIMEOUT)
        if ready is None:
            raise RuntimeError("agent-browser-cli exited before sending ready signal")
        try:
            data = json.loads(ready)
        except json.JSONDecodeError:
            raise RuntimeError(f"Invalid ready payload: {ready}")
        if data.get("status") != "ready":
            raise RuntimeError(f"Unexpected ready payload: {data}")

    async def send_command(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Send a JSON command and return the parsed response."""
        async with self._lock:
            if self._proc is None or self._proc.stdin is None:
                raise RuntimeError("Browser subprocess not running")

            command = {"action": action, **kwargs}
            payload = json.dumps(command, separators=(",", ":")) + "\n"

            try:
                self._proc.stdin.write(payload.encode())
                await self._proc.stdin.drain()
            except BrokenPipeError as exc:
                raise RuntimeError(f"Stdin pipe broken: {exc}") from exc

            raw = await self._read_line(timeout=COMMAND_TIMEOUT)
            if raw is None:
                raise RuntimeError(f"No response for action '{action}' (process exited)")

            try:
                resp = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"Invalid JSON response: {raw}")

            if resp.get("status") == "error":
                raise RuntimeError(resp.get("error", f"Action '{action}' failed"))

            return resp

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None
        if hasattr(self, "_stderr_task"):
            self._stderr_task.cancel()

    async def _read_line(self, timeout: float) -> str | None:
        """Read one JSON line from stdout with timeout."""
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            return None
        if not raw:
            return None
        return raw.decode(errors="replace").strip()

    async def _drain_stderr(self) -> None:
        """Background task: capture stderr (not exposed but prevents pipe buffer lock)."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
        except (asyncio.CancelledError, ValueError):
            pass


# ── MCP Server ─────────────────────────────────────────────────────────

# Pre-defined tools
TOOLS = [
    Tool(
        name="navigate",
        description="Navigate the browser to a URL and return the page's accessibility tree",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The full URL to navigate to (e.g. https://www.example.com)",
                }
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="click",
        description="Click an interactive element by its agent-assigned ID (e.g. e5). Returns updated accessibility tree.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "Element ID from the accessibility tree (e.g. 'e5', 'e12')",
                }
            },
            "required": ["target_id"],
        },
    ),
    Tool(
        name="type_text",
        description="Type text into an input element by its agent-assigned ID (e.g. e3). Simulates human-like keystroke delays.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "Element ID from the accessibility tree (e.g. 'e3')",
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the element",
                },
            },
            "required": ["target_id", "text"],
        },
    ),
    Tool(
        name="screenshot",
        description="Capture a screenshot of the current page. Returns the file path to the saved PNG image.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="page_tree",
        description="Extract the current page's accessibility tree without navigation. Returns structured text of interactive elements.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="page_meta",
        description="Get current page metadata (title, URL, interactive element count).",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="get_prompt",
        description="Get the built-in AI agent system prompt for browser automation (Chinese). Useful for LLM-based agents.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="configure",
        description="Configure runtime settings. Set media_enabled=true to allow all resources (images, CSS, fonts) to load. Useful for SPAs that need full rendering.",
        inputSchema={
            "type": "object",
            "properties": {
                "media_enabled": {
                    "type": "boolean",
                    "description": "Set to true to enable media resources, false to re-enable blocking",
                }
            },
            "required": ["media_enabled"],
        },
    ),
    Tool(
        name="evaluate",
        description="Execute arbitrary JavaScript on the current page and return the result. Bypasses the 50-char truncation of extract_tree.",
        inputSchema={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate on the page",
                }
            },
            "required": ["expression"],
        },
    ),
    Tool(
        name="download_setup",
        description="Set up download directory. Browser will intercept downloads and save files to this path.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to save downloaded files",
                }
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="download",
        description="Click a download link and save the file. The download_setup must be called first.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_id": {
                    "type": "string",
                    "description": "Element ID of the download link",
                }
            },
            "required": ["target_id"],
        },
    ),
    Tool(
        name="run_task",
        description="Execute a natural-language browser task. The Agent autonomously plans and executes multi-step operations (observe page tree -> decide next action via LLM -> execute -> observe) until the task is complete.",
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The browser task to complete, described in natural language",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Maximum reasoning/action steps (default 15)",
                },
                "llm_provider": {
                    "type": "string",
                    "description": "LLM provider: 'anthropic' or 'openai' (default: read LLM_PROVIDER env, else anthropic)",
                },
            },
            "required": ["task"],
        },
    ),
]


class AgentBrowserMCPServer:
    """MCP server wrapping agent-browser-cli."""

    def __init__(self) -> None:
        self._browser = BrowserSubprocess()
        self._server = Server("agent-browser")

    async def run(self) -> None:
        self._register_handlers()
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(read_stream, write_stream, self._server.create_initialization_options())

    def _register_handlers(self) -> None:
        s = self._server
        browser = self._browser

        @s.list_tools()
        async def list_tools():
            return TOOLS

        @s.call_tool()
        async def call_tool(name: str, args: dict[str, Any]):
            # Auto-start on first call
            if browser._proc is None:
                await browser.start()

            try:
                result = await dispatch(browser, name, args)
                # Return as TextContent list (unstructured content)
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
            except RuntimeError as e:
                # Return error as CallToolResult with isError flag
                raise RuntimeError(str(e))


async def dispatch(browser: BrowserSubprocess, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "navigate":
        url = args.get("url", "")
        if not url:
            raise RuntimeError("Missing required argument: 'url'")
        return await browser.send_command("navigate", url=url)

    if name == "click":
        target_id = args.get("target_id", "")
        if not target_id:
            raise RuntimeError("Missing required argument: 'target_id'")
        return await browser.send_command("click", target_id=target_id)

    if name == "type_text":
        target_id = args.get("target_id", "")
        text = args.get("text", "")
        if not target_id or not text:
            raise RuntimeError("Missing required arguments: 'target_id' and/or 'text'")
        return await browser.send_command("type", target_id=target_id, text=text)

    if name == "screenshot":
        return await browser.send_command("screenshot")

    if name == "page_tree":
        return await browser.send_command("tree")

    if name == "page_meta":
        return await browser.send_command("meta")

    if name == "get_prompt":
        return await browser.send_command("get_prompt")

    if name == "configure":
        media_enabled = args.get("media_enabled", False)
        return await browser.send_command("configure", media_enabled=media_enabled)

    if name == "evaluate":
        expression = args.get("expression", "")
        if not expression:
            raise RuntimeError("Missing required argument: 'expression'")
        return await browser.send_command("evaluate", expression=expression)

    if name == "get_content":
        return await browser.send_command("get_content")

    if name == "download_setup":
        path = args.get("path", "")
        if not path:
            raise RuntimeError("Missing required argument: 'path'")
        return await browser.send_command("download_setup", path=path)

    if name == "download":
        target_id = args.get("target_id", "")
        if not target_id:
            raise RuntimeError("Missing required argument: 'target_id'")
        return await browser.send_command("download", target_id=target_id)

    if name == "run_task":
        task = args.get("task", "")
        if not task:
            raise RuntimeError("Missing required argument: 'task'")
        max_steps = args.get("max_steps", 15)
        llm_provider = args.get("llm_provider", "") or os.environ.get("LLM_PROVIDER", "anthropic")

        # 创建适配器，让 BrowserSubprocess 适配 BrowserClient 接口
        adapter = _BrowserSubprocessAdapter(browser)
        from llm import LLMClient
        from agent_runner import AgentRunner

        llm = LLMClient(provider=llm_provider)
        runner = AgentRunner(browser=adapter, llm=llm, max_steps=max_steps)
        result = await runner.run(task)
        return {"action": "run_task", "status": "ok", "result": result.to_dict()}

    raise RuntimeError(f"Unknown tool: {name}")


# ── BrowserSubprocess 适配器 ──────────────────────────────────────────────


class _BrowserSubprocessAdapter:
    """将 BrowserSubprocess（send_command）适配为 agent_runner 所需的 BrowserClient 接口。

    agent_runner 调用 browser.meta(), browser.tree(), browser.navigate() 等方法，
    这个适配器把它们映射到 BrowserSubprocess.send_command()。
    """

    def __init__(self, browser: BrowserSubprocess) -> None:
        self._browser = browser

    async def meta(self) -> dict[str, Any]:
        resp = await self._browser.send_command("meta")
        return resp.get("meta", {})

    async def tree(self) -> str:
        resp = await self._browser.send_command("tree")
        return resp.get("tree", "[No interactive elements found]")

    async def navigate(self, url: str, **kw: Any) -> dict[str, Any]:
        return await self._browser.send_command("navigate", url=url)

    async def click(self, target_id: str, **kw: Any) -> dict[str, Any]:
        return await self._browser.send_command("click", target_id=target_id)

    async def type_text(self, target_id: str, text: str, **kw: Any) -> dict[str, Any]:
        return await self._browser.send_command("type", target_id=target_id, text=text)

    async def send_command(self, action: str, **kwargs: Any) -> dict[str, Any]:
        return await self._browser.send_command(action, **kwargs)


# ── Entrypoint ─────────────────────────────────────────────────────────


def main() -> None:
    server = AgentBrowserMCPServer()
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
