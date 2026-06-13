"""
Agent Browser Client - Python async SDK for agent-browser-cli.

Usage:
    from agent_browser_client import AgentBrowserClient

    async with AgentBrowserClient() as client:
        await client.navigate("https://example.com")
        tree = await client.get_tree()
        print(tree)
        await client.click("e5")
        await client.screenshot()
"""

import asyncio
import json
import sys
from typing import Any, Dict, List, Optional


class AgentBrowserError(Exception):
    """Error from agent-browser-cli."""
    pass


class AgentBrowserClient:
    """
    Async client for the agent-browser-cli Rust executable.

    Manages the CLI process lifecycle and provides a clean async interface
    for browser automation commands.
    """

    # Default timeout for individual commands (seconds)
    DEFAULT_COMMAND_TIMEOUT: float = 60.0

    def __init__(
        self,
        executable_path: Optional[str] = None,
        profile_path: Optional[str] = None,
        block_resources: bool = True,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ):
        """
        Initialize the client.

        Args:
            executable_path: Path to agent-browser-cli executable.
            profile_path: Chrome profile path for session reuse.
            block_resources: Whether to block images, CSS, fonts, ads for speed.
            command_timeout: Default timeout in seconds for each command.
        """
        self.executable_path = executable_path or self._find_executable()
        self.profile_path = profile_path
        self.block_resources = block_resources
        self.command_timeout = command_timeout

        self._process: Optional[asyncio.subprocess.Process] = None
        self._ready = False
        self._stderr_lines: List[str] = []
        self._stderr_task: Optional[asyncio.Task] = None

    def _find_executable(self) -> str:
        """Find the agent-browser-cli executable."""
        from pathlib import Path

        candidates = [
            Path("target/release/agent-browser-cli.exe"),
            Path("target/release/agent-browser-cli"),
            Path("target/debug/agent-browser-cli.exe"),
            Path("target/debug/agent-browser-cli"),
            Path("agent-browser-cli.exe"),
            Path("agent-browser-cli"),
        ]

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        name = "agent-browser-cli.exe" if sys.platform == "win32" else "agent-browser-cli"
        return name

    async def start(self) -> None:
        """Start the CLI process in listen mode."""
        if self._process is not None:
            raise AgentBrowserError("Client already started")

        cmd = [self.executable_path, "listen"]

        if self.profile_path:
            cmd.extend(["--profile", self.profile_path])

        if not self.block_resources:
            cmd.extend(["--block-resources", "false"])

        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )

        # Start stderr capture task
        self._stderr_task = asyncio.create_task(self._capture_stderr())

        # Wait for ready signal with timeout
        try:
            ready_line = await asyncio.wait_for(
                self._read_line(), timeout=30.0
            )
        except asyncio.TimeoutError:
            await self._kill_process()
            raise AgentBrowserError("CLI did not send ready signal within 30s")

        if ready_line is None:
            stderr_dump = "\n".join(self._stderr_lines[-20:])
            await self._kill_process()
            raise AgentBrowserError(
                f"CLI process exited before ready signal.\nStderr:\n{stderr_dump}"
            )

        ready_data = json.loads(ready_line)
        if ready_data.get("status") != "ready":
            raise AgentBrowserError(f"Unexpected ready signal: {ready_data}")

        self._ready = True

    async def stop(self) -> None:
        """Stop the CLI process gracefully."""
        if self._process is None:
            return

        self._ready = False

        try:
            if self._process.stdin and not self._process.stdin.is_closing():
                self._process.stdin.close()
                try:
                    await asyncio.wait_for(
                        self._process.stdin.wait_closed(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    pass  # stdin didn't close, proceed to kill
        except (BrokenPipeError, ConnectionResetError):
            pass

        await self._kill_process()

        # Cancel stderr capture
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

    async def _kill_process(self) -> None:
        """Force-kill the CLI process with timeout."""
        if self._process is None:
            return

        try:
            self._process.kill()
        except ProcessLookupError:
            pass  # Already exited

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass  # Still running after kill — OS will reap

        self._process = None

    async def _capture_stderr(self) -> None:
        """Background task: capture stderr lines for diagnostics."""
        if self._process is None or self._process.stderr is None:
            return
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="replace").strip()
                if msg:
                    self._stderr_lines.append(msg)
                    # Cap buffer at 500 lines
                    if len(self._stderr_lines) > 500:
                        self._stderr_lines.pop(0)
        except asyncio.CancelledError:
            pass

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False

    async def send_command(
        self,
        command: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Send a JSON command and receive the JSON response.

        Args:
            command: Dictionary to send as JSON.
            timeout: Override default timeout for this command (seconds).

        Returns:
            Response dictionary from the CLI.

        Raises:
            AgentBrowserError: On timeout, pipe failure, or CLI error.
        """
        if not self._ready or self._process is None:
            raise AgentBrowserError("Client not started. Call start() first.")

        effective_timeout = timeout or self.command_timeout

        # Send command
        cmd_json = json.dumps(command) + "\n"
        try:
            self._process.stdin.write(cmd_json.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise AgentBrowserError(f"Failed to send command (pipe broken): {e}")

        # Read response with timeout
        try:
            line = await asyncio.wait_for(
                self._read_line(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            raise AgentBrowserError(
                f"Command timed out after {effective_timeout}s: "
                f"{command.get('action', '?')}"
            )

        if line is None:
            stderr_dump = "\n".join(self._stderr_lines[-10:])
            raise AgentBrowserError(
                f"CLI process exited (no response).\nStderr:\n{stderr_dump}"
            )

        try:
            response = json.loads(line)
        except json.JSONDecodeError as e:
            raise AgentBrowserError(f"Invalid JSON response: {line!r}") from e

        if response.get("status") == "error":
            raise AgentBrowserError(response.get("error", "Unknown error"))

        return response

    async def _read_line(self) -> Optional[str]:
        """Read a single line from stdout. Returns None on EOF."""
        if self._process is None or self._process.stdout is None:
            return None

        try:
            line = await self._process.stdout.readline()
            if not line:
                return None
            return line.decode().strip()
        except (BrokenPipeError, ConnectionResetError):
            return None

    # ─── Convenience Methods ────────────────────────────────────────────

    async def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate to a URL and return the page tree."""
        return await self.send_command({"action": "navigate", "url": url})

    async def click(self, target_id: str) -> Dict[str, Any]:
        """Click an element by its agent-id."""
        return await self.send_command({
            "action": "click",
            "target_id": target_id,
        })

    async def type_text(self, target_id: str, text: str) -> Dict[str, Any]:
        """Type text into an element by its agent-id."""
        return await self.send_command({
            "action": "type",
            "target_id": target_id,
            "text": text,
        })

    async def screenshot(self) -> str:
        """Take a screenshot. Returns the file path."""
        resp = await self.send_command({"action": "screenshot"})
        return resp.get("path", "")

    async def get_tree(self) -> str:
        """Extract the accessibility tree without navigating."""
        resp = await self.send_command({"action": "tree"})
        return resp.get("tree", "")

    async def get_meta(self) -> Dict[str, Any]:
        """Get page metadata (title, url, element count)."""
        resp = await self.send_command({"action": "meta"})
        return resp.get("meta", {})

    async def configure(self, media_enabled: bool) -> Dict[str, Any]:
        """Toggle media blocking dynamically."""
        return await self.send_command({
            "action": "configure",
            "media_enabled": media_enabled,
        })

    async def safe_screenshot(self) -> Optional[str]:
        """Take a screenshot, catching any errors."""
        try:
            return await self.screenshot()
        except AgentBrowserError:
            return None

    @property
    def stderr_log(self) -> List[str]:
        """Return captured stderr lines (read-only copy)."""
        return list(self._stderr_lines)


# ─── Quick Test ──────────────────────────────────────────────────────────

async def main():
    """Quick demo of the client."""
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"

    print("[*] Starting browser client...")
    async with AgentBrowserClient() as client:
        print(f"[*] Navigating to {url}...")
        result = await client.navigate(url)
        print(f"[*] Title: {result.get('title', 'N/A')}")
        print(f"[*] Interactive elements: {result.get('interactive_count', 0)}")
        print(f"[*] Tree:\n{result.get('tree', 'empty')}")

        path = await client.screenshot()
        print(f"[*] Screenshot saved: {path}")

    print("[*] Done.")


if __name__ == "__main__":
    asyncio.run(main())
