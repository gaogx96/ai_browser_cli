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
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# 日志输出到 stderr，避免污染 MCP 的 stdout JSON-RPC 协议
_log = lambda *a, **kw: print(*a, **kw, file=sys.stderr, flush=True)

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


# ── 阶段 6B：CheckpointStore + Session Registry ───────────────────────────
# 同一进程内恢复。跨进程持久化暂不实现。


class CheckpointStatus(str, Enum):
    PAUSED = "paused"
    RESUMING = "resuming"
    RESUMED = "resumed"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class StoredCheckpoint:
    """带状态的 checkpoint 记录（内存存储）。"""
    checkpoint_id: str
    session_id: str
    status: CheckpointStatus = CheckpointStatus.PAUSED
    created_at: float = 0.0
    data: dict = field(default_factory=dict)  # checkpoint.to_dict()


class CheckpointStore:
    """内存 checkpoint 存储：put / get / consume / expire。

    使用 asyncio.Lock 保证 consume() 的原子性（防止并发重复恢复）。
    TTL 过期：创建时设置 TTL，读取时惰性检查。后台清理任务定期扫描。
    """

    TTL_SECONDS = 3600  # 默认 1 小时过期

    def __init__(self) -> None:
        self._store: dict[str, StoredCheckpoint] = {}
        self._lock = asyncio.Lock()

    async def put(self, checkpoint: Any, session_id: str) -> str:
        """保存 checkpoint，返回 checkpoint_id。"""
        import time
        ck_id = checkpoint.checkpoint_id or secrets.token_urlsafe(24)
        self._store[ck_id] = StoredCheckpoint(
            checkpoint_id=ck_id,
            session_id=session_id,
            status=CheckpointStatus.PAUSED,
            created_at=time.time(),
            data=checkpoint.to_dict(),
        )
        return ck_id

    async def get(self, checkpoint_id: str) -> StoredCheckpoint | None:
        ck = self._store.get(checkpoint_id)
        if ck is None:
            return None
        # 惰性过期检查
        import time
        if ck.status in (CheckpointStatus.PAUSED, CheckpointStatus.RESUMING) and \
           time.time() - ck.created_at > self.TTL_SECONDS:
            ck.status = CheckpointStatus.EXPIRED
        return ck

    async def consume(self, checkpoint_id: str) -> StoredCheckpoint | None:
        """原子领取：把 checkpoint 从 paused 改为 resuming，防止并发重复恢复。

        asyncio.Lock 保证 check-then-act 的原子性。
        只在状态为 PAUSED 时成功领取（返回对象并置为 RESUMING）。
        否则返回 None（已被领取/已恢复/已过期），调用方据此判断冲突。
        """
        async with self._lock:
            ck = self._store.get(checkpoint_id)
            if ck is None:
                return None
            # 惰性过期检查
            import time
            if time.time() - ck.created_at > self.TTL_SECONDS:
                ck.status = CheckpointStatus.EXPIRED
            if ck.status != CheckpointStatus.PAUSED:
                return None  # 非 PAUSED，无法领取
            ck.status = CheckpointStatus.RESUMING
            return ck

    async def mark_resumed(self, checkpoint_id: str) -> None:
        if checkpoint_id in self._store:
            self._store[checkpoint_id].status = CheckpointStatus.RESUMED

    async def mark_superseded(self, checkpoint_id: str) -> None:
        if checkpoint_id in self._store:
            self._store[checkpoint_id].status = CheckpointStatus.SUPERSEDED

    async def expire(self, checkpoint_id: str) -> None:
        if checkpoint_id in self._store:
            self._store[checkpoint_id].status = CheckpointStatus.EXPIRED

    async def cleanup_expired(self) -> int:
        """清理所有过期 checkpoint（从 store 中移除）。返回清理数量。"""
        import time
        now = time.time()
        expired_ids = [
            ck_id for ck_id, ck in self._store.items()
            if ck.status in (CheckpointStatus.EXPIRED, CheckpointStatus.RESUMED, CheckpointStatus.SUPERSEDED)
            or (now - ck.created_at > self.TTL_SECONDS * 2)
        ]
        for ck_id in expired_ids:
            del self._store[ck_id]
        return len(expired_ids)


@dataclass
class AgentSession:
    """一个活动的 agent 会话（同一进程内复用）。"""
    session_id: str
    browser: BrowserSubprocess
    adapter: "_BrowserSubprocessAdapter"
    runner: Any
    checkpoint_id: str | None = None


class SessionRegistry:
    """session_id → AgentSession 映射。恢复时复用原浏览器会话。

    session 在以下情况自动清理：
    - 任务正常完成（run_task 返回 success）
    - 任务最终失败（run_task 返回 failed）
    - resume 失败（恢复异常）
    - 显式 remove()
    - cleanup_stale() 定期清理（用于浏览器断开等）
    """

    TTL_SECONDS = 3600  # 默认 1 小时无活动自动清理

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._last_activity: dict[str, float] = {}

    def register(self, session: AgentSession) -> None:
        self._sessions[session.session_id] = session
        import time
        self._last_activity[session.session_id] = time.time()

    def get(self, session_id: str) -> AgentSession | None:
        if session_id in self._sessions:
            import time
            self._last_activity[session_id] = time.time()
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._last_activity.pop(session_id, None)

    def has(self, session_id: str) -> bool:
        return session_id in self._sessions

    async def cleanup_stale(self) -> int:
        """清理过期 session（TTL 超时）。返回清理数量。"""
        import time
        now = time.time()
        stale_ids = [
            sid for sid, last in self._last_activity.items()
            if now - last > self.TTL_SECONDS
        ]
        for sid in stale_ids:
            self.remove(sid)
        return len(stale_ids)


# MCP 错误码
class ResumeErrorCode(str, Enum):
    CHECKPOINT_NOT_FOUND = "CHECKPOINT_NOT_FOUND"
    CHECKPOINT_EXPIRED = "CHECKPOINT_EXPIRED"
    CHECKPOINT_ALREADY_RESUMED = "CHECKPOINT_ALREADY_RESUMED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    CHECKPOINT_VERSION_UNSUPPORTED = "CHECKPOINT_VERSION_UNSUPPORTED"
    PAGE_CONTEXT_UNAVAILABLE = "PAGE_CONTEXT_UNAVAILABLE"
    RESUME_CONFLICT = "RESUME_CONFLICT"


class ResumeError(Exception):
    def __init__(self, code: ResumeErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
    Tool(
        name="agent_resume",
        description="Resume a paused browser task from a checkpoint. The Agent re-observes the current page, matches it against the checkpoint, restores task state, and continues from the LLM decision loop. Does NOT replay old actions. Same-process only: the original browser session must still be alive.",
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "The checkpoint ID returned by a previous run_task that paused",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Optional override for max steps during resume",
                },
            },
            "required": ["checkpoint_id"],
        },
    ),
]


class AgentBrowserMCPServer:
    """MCP server wrapping agent-browser-cli."""

    def __init__(self) -> None:
        self._browser = BrowserSubprocess()
        self._server = Server("agent-browser")
        self._checkpoint_store = CheckpointStore()
        self._session_registry = SessionRegistry()
        self._cleanup_task: asyncio.Task | None = None

    async def _cleanup_loop(self) -> None:
        """后台定期清理任务：每 5 分钟清理过期 checkpoint 和 session。"""
        try:
            while True:
                await asyncio.sleep(300)  # 5 分钟
                ck_cleaned = await self._checkpoint_store.cleanup_expired()
                sess_cleaned = await self._session_registry.cleanup_stale()
                if ck_cleaned or sess_cleaned:
                    _log(f"[cleanup] removed {ck_cleaned} checkpoints, {sess_cleaned} sessions")
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        self._register_handlers()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self._server.run(read_stream, write_stream, self._server.create_initialization_options())
        finally:
            if self._cleanup_task and not self._cleanup_task.done():
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass

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
                result = await dispatch(
                    browser,
                    name,
                    args,
                    checkpoint_store=self._checkpoint_store,
                    session_registry=self._session_registry,
                )
                # Return as TextContent list (unstructured content)
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
            except RuntimeError as e:
                # Return error as CallToolResult with isError flag
                raise RuntimeError(str(e))


async def dispatch(
    browser: BrowserSubprocess,
    name: str,
    args: dict[str, Any],
    checkpoint_store: CheckpointStore | None = None,
    session_registry: SessionRegistry | None = None,
) -> dict[str, Any]:
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

        # 如果暂停，保存 checkpoint 和 session
        if result.status == "paused" and result.checkpoint and checkpoint_store and session_registry:
            session_id = result.checkpoint.session_id or secrets.token_urlsafe(24)
            ck_id = await checkpoint_store.put(result.checkpoint, session_id)
            session_registry.register(AgentSession(
                session_id=session_id,
                browser=browser,
                adapter=adapter,
                runner=runner,
                checkpoint_id=ck_id,
            ))
            result_dict = result.to_dict()
            result_dict["checkpoint"]["checkpoint_id"] = ck_id
            result_dict["session_id"] = session_id
            return {"action": "run_task", "status": "ok", "result": result_dict}

        return {"action": "run_task", "status": "ok", "result": result.to_dict()}

    if name == "agent_resume":
        if not checkpoint_store or not session_registry:
            raise RuntimeError("Checkpoint store or session registry not available")
        return await _handle_resume(browser, args, checkpoint_store, session_registry)

    raise RuntimeError(f"Unknown tool: {name}")


async def _handle_resume(
    browser: BrowserSubprocess,
    args: dict[str, Any],
    checkpoint_store: CheckpointStore,
    session_registry: SessionRegistry,
) -> dict[str, Any]:
    """处理 agent_resume 请求。同一进程内恢复，复用原浏览器 session。"""
    checkpoint_id = args.get("checkpoint_id", "")
    if not checkpoint_id:
        raise ResumeError(ResumeErrorCode.CHECKPOINT_NOT_FOUND, "Missing checkpoint_id")

    # 1. 读取 checkpoint
    stored = await checkpoint_store.get(checkpoint_id)
    if stored is None:
        raise ResumeError(ResumeErrorCode.CHECKPOINT_NOT_FOUND, f"Checkpoint {checkpoint_id} not found")

    if stored.status == CheckpointStatus.EXPIRED:
        raise ResumeError(ResumeErrorCode.CHECKPOINT_EXPIRED, f"Checkpoint {checkpoint_id} has expired")
    if stored.status == CheckpointStatus.RESUMED:
        raise ResumeError(ResumeErrorCode.CHECKPOINT_ALREADY_RESUMED, f"Checkpoint {checkpoint_id} already resumed")
    if stored.status == CheckpointStatus.RESUMING:
        raise ResumeError(ResumeErrorCode.RESUME_CONFLICT, f"Checkpoint {checkpoint_id} is being resumed by another request")

    # 2. 查找 session
    session = session_registry.get(stored.session_id)
    if session is None:
        raise ResumeError(ResumeErrorCode.SESSION_NOT_FOUND, f"Session {stored.session_id} no longer exists")

    # 3. 原子领取 checkpoint（防止并发）
    claimed = await checkpoint_store.consume(checkpoint_id)
    if claimed is None:
        # 检查原因：不存在 / 已领取 / 已过期
        stored2 = await checkpoint_store.get(checkpoint_id)
        if stored2 is None:
            raise ResumeError(ResumeErrorCode.CHECKPOINT_NOT_FOUND, f"Checkpoint {checkpoint_id} not found")
        if stored2.status == CheckpointStatus.EXPIRED:
            raise ResumeError(ResumeErrorCode.CHECKPOINT_EXPIRED, f"Checkpoint {checkpoint_id} has expired")
        if stored2.status in (CheckpointStatus.RESUMED, CheckpointStatus.RESUMING):
            raise ResumeError(ResumeErrorCode.RESUME_CONFLICT, f"Checkpoint {checkpoint_id} already claimed by another request")
        raise ResumeError(ResumeErrorCode.RESUME_CONFLICT, f"Checkpoint {checkpoint_id} unavailable (status={stored2.status})")

    # 4. 从 checkpoint 数据重建 Checkpoint 对象
    from agent_runner import Checkpoint as AgentCheckpoint
    ck_data = stored.data
    checkpoint = AgentCheckpoint(
        version=ck_data.get("version", 1),
        checkpoint_id=checkpoint_id,
        session_id=stored.session_id,
        task=ck_data.get("task", ""),
        current_goal=ck_data.get("current_goal"),
        next_goal=ck_data.get("next_goal"),
        completed_goals=ck_data.get("completed_goals", []),
        failed_attempts=ck_data.get("failed_attempts", []),
        step=ck_data.get("step", 0),
        page_url=ck_data.get("page_url", ""),
        page_fingerprint=ck_data.get("page_fingerprint"),
        pause_reason=ck_data.get("pause_reason", "waiting_for_user"),
        snapshot_available=ck_data.get("snapshot_available", True),
    )

    # 5. 恢复执行
    try:
        result = await session.runner.resume(checkpoint)
        # 标记 checkpoint 已恢复
        if result.status == "paused" and result.checkpoint:
            await checkpoint_store.mark_resumed(checkpoint_id)
            await checkpoint_store.mark_superseded(checkpoint_id)
            # 新 pause 创建新 checkpoint
            new_ck_id = await checkpoint_store.put(result.checkpoint, stored.session_id)
            session_registry.register(AgentSession(
                session_id=stored.session_id,
                browser=browser,
                adapter=session.adapter,
                runner=session.runner,
                checkpoint_id=new_ck_id,
            ))
            result_dict = result.to_dict()
            result_dict["checkpoint"]["checkpoint_id"] = new_ck_id
            result_dict["session_id"] = stored.session_id
            return {"action": "agent_resume", "status": "ok", "result": result_dict}
        else:
            await checkpoint_store.mark_resumed(checkpoint_id)
            return {"action": "agent_resume", "status": "ok", "result": result.to_dict()}
    except Exception as e:
        # resume 失败：标记 checkpoint 为 failed（而非 expired），
        # 避免永久停留在 resuming 状态
        if checkpoint_id:
            try:
                ck = await checkpoint_store.get(checkpoint_id)
                if ck and ck.status == CheckpointStatus.RESUMING:
                    ck.status = CheckpointStatus.FAILED
            except Exception:
                pass
        raise RuntimeError(f"Resume failed: {e}")


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
