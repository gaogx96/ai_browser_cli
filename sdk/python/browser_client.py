"""
Agent Browser Client — Python async SDK for agent-browser-cli v0.5+.

Supports two modes:
  1. Launch mode: spawns a new Chrome instance.
  2. Connect mode: attaches to an existing Chrome via --remote-debugging-port.

Usage (launch mode):
    async with BrowserClient() as client:
        result = await client.navigate("https://example.com")
        print(result["tree"])

Usage (connect mode — reuses human login sessions):
    async with BrowserClient(connect="http://127.0.0.1:9222") as client:
        result = await client.navigate("https://www.github.com")
        print(result["tree"])

Usage (manual lifecycle):
    client = BrowserClient()
    await client.start()
    try:
        await client.navigate("https://example.com")
    finally:
        await client.close()
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


class BrowserClientError(Exception):
    """Raised when the CLI returns an error or the pipe breaks."""
    pass


class BrowserClient:
    """
    Async client that manages an agent-browser-cli subprocess and provides
    a clean JSON-over-stdin/stdout interface for browser automation.
    """

    DEFAULT_TIMEOUT: float = 60.0
    READY_TIMEOUT: float = 30.0

    def __init__(
        self,
        executable: Optional[str] = None,
        connect: Optional[str] = None,
        profile: Optional[str] = None,
        resources: str = "block",
        show: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Args:
            executable:  Path to agent-browser-cli binary. Auto-detected if None.
            connect:     Chrome debugging URL (e.g. "http://127.0.0.1:9222").
                         When set, attaches to existing Chrome instead of launching.
            profile:     Chrome user-data-dir for session reuse.
            resources:   Resource loading strategy: "block" (default), "allow", "smart".
            show:        Show browser window (disable headless).
            timeout:     Default per-command timeout in seconds.
        """
        self._executable = executable or self._find_executable()
        self._connect = connect
        self._profile = profile
        self._resources = resources
        self._show = show
        self._timeout = timeout

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._ready = False
        self._stderr_lines: List[str] = []
        self._stderr_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> "BrowserClient":
        """Start the CLI subprocess. Call once before sending commands."""
        if self._proc is not None:
            raise BrowserClientError("Client already started")

        cmd = [self._executable, "listen"]

        if self._connect:
            cmd.extend(["--connect", self._connect])
        if self._profile:
            cmd.extend(["--profile", self._profile])
        cmd.extend(["--resources", self._resources])
        if self._show:
            cmd.append("--show")

        creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )

        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # Wait for {"status": "ready"} signal
        try:
            ready = await self._read_json(timeout=self.READY_TIMEOUT)
        except asyncio.TimeoutError:
            await self._kill()
            raise BrowserClientError("CLI did not send ready signal within timeout")

        if ready is None:
            tail = "\n".join(self._stderr_lines[-20:])
            await self._kill()
            raise BrowserClientError(f"CLI exited before ready.\nStderr:\n{tail}")

        if ready.get("status") != "ready":
            raise BrowserClientError(f"Unexpected ready payload: {ready}")

        self._ready = True
        return self

    async def close(self) -> None:
        """Gracefully shut down the CLI subprocess."""
        if self._proc is None:
            return
        self._ready = False

        # Close stdin to signal EOF → CLI exits its listen loop
        try:
            if self._proc.stdin and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.stdin.wait_closed(), timeout=2.0)
        except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
            pass

        await self._kill()

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self) -> "BrowserClient":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── Command interface ──────────────────────────────────────────────

    async def send_command(
        self,
        action: str,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Send a JSON command and return the parsed response.

        Args:
            action:  The command name (e.g. "navigate", "click", "tree").
            timeout: Per-command timeout override (seconds).
            **kwargs: Additional command parameters.

        Returns:
            Parsed JSON response dict.

        Raises:
            BrowserClientError: On pipe failure, timeout, or CLI error.
        """
        if not self._ready or self._proc is None:
            raise BrowserClientError("Client not started. Call start() first.")

        command = {"action": action, **kwargs}
        payload = json.dumps(command, separators=(",", ":")) + "\n"

        # Write to stdin
        try:
            self._proc.stdin.write(payload.encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise BrowserClientError(f"Stdin pipe broken: {exc}") from exc

        # Read response
        effective_timeout = timeout or self._timeout
        try:
            resp = await self._read_json(timeout=effective_timeout)
        except asyncio.TimeoutError:
            # Attempt emergency screenshot before declaring timeout
            await self._try_emergency_screenshot()
            raise BrowserClientError(
                f"Command '{action}' timed out after {effective_timeout}s"
            )

        if resp is None:
            tail = "\n".join(self._stderr_lines[-10:])
            raise BrowserClientError(
                f"CLI exited without response.\nStderr:\n{tail}"
            )

        if resp.get("status") == "error":
            raise BrowserClientError(resp.get("error", "Unknown CLI error"))

        return resp

    # ── Convenience methods ────────────────────────────────────────────

    async def navigate(self, url: str, **kw: Any) -> Dict[str, Any]:
        """Navigate to URL. Returns {title, tree, interactive_count, ...}."""
        return await self.send_command("navigate", url=url, **kw)

    async def click(self, target_id: str, **kw: Any) -> Dict[str, Any]:
        """Click element by agent-id (e.g. "e5"). Returns {scrolled, tree}."""
        return await self.send_command("click", target_id=target_id, **kw)

    async def type_text(self, target_id: str, text: str, **kw: Any) -> Dict[str, Any]:
        """Type text into element. Returns {tree}."""
        return await self.send_command("type", target_id=target_id, text=text, **kw)

    async def screenshot(self, **kw: Any) -> str:
        """Capture screenshot. Returns file path."""
        resp = await self.send_command("screenshot", **kw)
        return resp.get("path", "")

    async def tree(self, **kw: Any) -> str:
        """Extract accessibility tree without navigation."""
        resp = await self.send_command("tree", **kw)
        return resp.get("tree", "")

    async def meta(self, **kw: Any) -> Dict[str, Any]:
        """Get page metadata (title, url, interactiveCount)."""
        resp = await self.send_command("meta", **kw)
        return resp.get("meta", {})

    async def configure(self, media_enabled: bool, **kw: Any) -> Dict[str, Any]:
        """Toggle media blocking at runtime."""
        return await self.send_command(
            "configure", media_enabled=media_enabled, **kw
        )

    async def safe_screenshot(self) -> Optional[str]:
        """Screenshot that never raises — returns None on failure."""
        try:
            return await self.screenshot()
        except Exception:
            return None

    @property
    def stderr_log(self) -> List[str]:
        """Return captured stderr lines."""
        return list(self._stderr_lines)

    # ── Internal helpers ───────────────────────────────────────────────

    async def _read_json(self, timeout: float) -> Optional[Dict[str, Any]]:
        """Read one JSON line from stdout with timeout. None on EOF."""
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            raw = await asyncio.wait_for(
                self._proc.stdout.readline(), timeout=timeout
            )
        except (BrokenPipeError, ConnectionResetError):
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def _drain_stderr(self) -> None:
        """Background task: capture stderr for diagnostics."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="replace").strip()
                if msg:
                    self._stderr_lines.append(msg)
                    if len(self._stderr_lines) > 500:
                        self._stderr_lines.pop(0)
        except asyncio.CancelledError:
            pass

    async def _try_emergency_screenshot(self) -> None:
        """Best-effort screenshot on error. Never raises."""
        try:
            if self._proc and self._proc.stdin and not self._proc.stdin.is_closing():
                cmd = json.dumps({"action": "screenshot"}) + "\n"
                self._proc.stdin.write(cmd.encode())
                await self._proc.stdin.drain()
        except Exception:
            pass

    async def _kill(self) -> None:
        """Force-kill subprocess with timeout."""
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        self._proc = None

    @staticmethod
    def _find_executable() -> str:
        """Auto-locate the agent-browser-cli binary."""
        is_win = sys.platform == "win32"
        ext = ".exe" if is_win else ""
        name = f"agent-browser-cli{ext}"

        candidates = [
            Path("target/release") / name,
            Path("target/debug") / name,
            Path(name),
        ]

        # Also check relative to this file's location
        here = Path(__file__).resolve().parent
        for parent in [here, *here.parents]:
            candidates.append(parent / "target" / "release" / name)
            candidates.append(parent / "target" / "debug" / name)

        for c in candidates:
            if c.exists():
                return str(c)

        # Fall back to PATH
        return name


# ── Quick test ─────────────────────────────────────────────────────────

async def _main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.baidu.com"
    connect = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"[*] Starting browser client (connect={connect})...")
    async with BrowserClient(connect=connect) as client:
        print(f"[*] Navigating to {url}...")
        result = await client.navigate(url)
        print(f"[*] Title: {result.get('title', 'N/A')}")
        print(f"[*] Elements: {result.get('interactive_count', 0)}")
        tree = result.get("tree", "")
        # Print first 20 lines of tree
        for line in tree.split("\n")[:20]:
            print(f"    {line}")
        if tree.count("\n") > 20:
            print(f"    ... ({tree.count(chr(10)) + 1} lines total)")

        path = await client.safe_screenshot()
        if path:
            print(f"[*] Screenshot: {path}")

    print("[*] Done.")


if __name__ == "__main__":
    asyncio.run(_main())
