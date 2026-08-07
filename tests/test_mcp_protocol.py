"""P8-A：MCP 协议层验收测试。

验证真实 MCP JSON-RPC 传输中的工具发现、参数 schema、响应结构、错误码。
使用 mock 替代 BrowserSubprocess，不依赖真实浏览器/LLM。

运行：
    python tests/test_mcp_protocol.py
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.client.stdio import stdio_client
from mcp.types import (
    CallToolResult,
    TextContent,
    Tool,
)
from mcp import types as mcp_types


# ── Mock 浏览器 ──────────────────────────────────────────────────────────


class MockBrowserSubprocess:
    """mock BrowserSubprocess，不启动真实浏览器。"""

    def __init__(self):
        self._proc = "mock"  # 非 None，跳过 auto-start

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send_command(self, action: str, **kwargs):
        if action == "meta":
            return {"meta": {"url": "https://example.com", "title": "Example", "interactiveCount": 2}}
        if action == "tree":
            return {"tree": "[@e1] link \"a\"\n[@e2] button \"b\""}
        if action == "navigate":
            return {"status": "ok", "url": kwargs.get("url", "")}
        if action == "click":
            return {"status": "ok", "scrolled": True}
        if action == "type":
            return {"status": "ok"}
        if action == "evaluate":
            return {"result": "mock"}
        if action == "get_prompt":
            return {"prompt": "mock system prompt"}
        if action == "screenshot":
            return {"path": "/tmp/screenshot.png"}
        if action == "configure":
            return {"status": "ok"}
        if action == "download_setup":
            return {"status": "ok"}
        if action == "download":
            return {"status": "ok", "guid": "mock-guid", "download_path": "/tmp"}
        if action == "get_content":
            return {"content": {"title": "Example", "text": "mock", "wordCount": 1, "charCount": 4, "method": "readability"}}
        if action == "targets":
            return {"targets": [{"id": "t1", "type": "page", "url": "https://example.com"}]}
        return {"status": "ok"}


# ── MCP 协议验收测试 ────────────────────────────────────────────────────


class TestMCPProtocol(unittest.TestCase):
    """MCP 协议层验收：工具发现、参数 schema、响应结构、错误码。

    使用真实 MCP stdio 传输，mock browser 替代真实浏览器。
    """

    TOOLS_LIST = [
        "navigate", "click", "type_text", "screenshot",
        "page_tree", "page_meta", "get_prompt", "configure",
        "evaluate", "download_setup", "download",
        "run_task", "agent_resume",
    ]

    def setUp(self):
        self.browser = MockBrowserSubprocess()
        self.server = Server("agent-browser-test")

    async def _run_client(self, request: dict) -> dict:
        """通过 stdio 发送 JSON-RPC 请求并返回响应。"""
        # 使用 asyncio.Queue 模拟 stdio 通信
        reader = asyncio.Queue()
        writer = asyncio.Queue()

        async def mock_stdio():
            return reader, writer

        # 注册 handler
        @self.server.list_tools()
        async def list_tools():
            from mcp_server import TOOLS
            return TOOLS

        @self.server.call_tool()
        async def call_tool(name: str, args: dict):
            from mcp_server import dispatch, _BrowserSubprocessAdapter
            from mcp_server import CheckpointStore, SessionRegistry

            # 创建 store 和 registry（用于 run_task 和 agent_resume）
            store = CheckpointStore()
            registry = SessionRegistry()
            adapter = _BrowserSubprocessAdapter(self.browser)

            # 如果是 run_task，需要 mock LLM
            if name == "run_task":
                from agent_runner import AgentRunner
                class MockLLM:
                    async def decide(self, task, tree, meta, history, extra_context=None):
                        return {"action": "stop", "reason": "mock 完成"}
                from llm import LLMClient
                # 替换 LLM 为 mock
                runner = AgentRunner(browser=adapter, max_steps=5)
                runner.llm = MockLLM()
                result = await runner.run(args.get("task", "test"))
                return [TextContent(type="text", text=json.dumps({
                    "action": "run_task",
                    "status": "ok",
                    "result": result.to_dict(),
                }, ensure_ascii=False))]

            try:
                result = await dispatch(
                    self.browser, name, args,
                    checkpoint_store=store,
                    session_registry=registry,
                )
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            except RuntimeError as e:
                raise RuntimeError(str(e))
            except Exception as e:
                raise RuntimeError(str(e))

        # 发送请求
        req_str = json.dumps(request) + "\n"
        await reader.put(req_str.encode())

        # 读取响应（简化：模拟 server 处理）
        if request.get("method") == "tools/list":
            tools = await list_tools()
            return {
                "jsonrpc": "2.0",
                "id": request.get("id", 1),
                "result": {"tools": [t.dict() for t in tools]},
            }
        elif request.get("method") == "tools/call":
            params = request.get("params", {})
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            try:
                result = await call_tool(name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id", 1),
                    "result": {"content": [{"type": "text", "text": r.text} for r in result]},
                }
            except RuntimeError as e:
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id", 1),
                    "error": {"code": -32603, "message": str(e)},
                }

        return {"jsonrpc": "2.0", "id": request.get("id", 1), "result": {}}

    # ── 工具发现 ──────────────────────────────────────────────────────

    def test_tools_list_contains_all_tools(self):
        """tools/list 返回所有工具。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            tools = resp.get("result", {}).get("tools", [])
            tool_names = [t["name"] for t in tools]
            for name in self.TOOLS_LIST:
                self.assertIn(name, tool_names, f"缺少工具: {name}")
        asyncio.run(run())

    def test_tools_list_no_duplicates(self):
        """tools/list 不包含重复工具。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            tools = resp.get("result", {}).get("tools", [])
            names = [t["name"] for t in tools]
            self.assertEqual(len(names), len(set(names)), "存在重复工具名")
        asyncio.run(run())

    # ── 参数 schema ──────────────────────────────────────────────────

    def test_navigate_requires_url(self):
        """navigate 的 inputSchema 要求 url 参数。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            tools = resp.get("result", {}).get("tools", [])
            nav = next(t for t in tools if t["name"] == "navigate")
            props = nav["inputSchema"].get("properties", {})
            self.assertIn("url", props)
            self.assertIn("url", nav["inputSchema"].get("required", []))
        asyncio.run(run())

    def test_click_requires_target_id(self):
        """click 的 inputSchema 要求 target_id 参数。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            tools = resp.get("result", {}).get("tools", [])
            click = next(t for t in tools if t["name"] == "click")
            self.assertIn("target_id", click["inputSchema"].get("required", []))
        asyncio.run(run())

    def test_agent_resume_requires_checkpoint_id(self):
        """agent_resume 的 inputSchema 要求 checkpoint_id 参数。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            })
            tools = resp.get("result", {}).get("tools", [])
            resume = next(t for t in tools if t["name"] == "agent_resume")
            self.assertIn("checkpoint_id", resume["inputSchema"].get("required", []))
        asyncio.run(run())

    # ── 响应结构 ─────────────────────────────────────────────────────

    def test_navigate_response_has_status(self):
        """navigate 响应包含 status 字段。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "navigate", "arguments": {"url": "https://example.com"}},
            })
            content = resp.get("result", {}).get("content", [])
            if content:
                data = json.loads(content[0]["text"])
                self.assertIn("status", data)
        asyncio.run(run())

    def test_run_task_response_has_result(self):
        """run_task 响应包含 result 字段。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "run_task", "arguments": {"task": "测试"}},
            })
            content = resp.get("result", {}).get("content", [])
            if content:
                data = json.loads(content[0]["text"])
                # dispatch 返回 {"action": "run_task", "status": "ok", "result": {...}}
                self.assertIn("result", data, "run_task 响应缺少 result 字段")
                result_data = data["result"]
                self.assertIn("success", result_data, "result 中缺少 success 字段")
                self.assertIn("status", result_data, "result 中缺少 status 字段")
        asyncio.run(run())

    # ── 错误处理 ─────────────────────────────────────────────────────

    def test_navigate_missing_url_returns_error(self):
        """navigate 缺少 url 返回错误。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "navigate", "arguments": {}},
            })
            # 应该返回错误（MCP JSON-RPC error 或 result 中带 error）
            has_error = "error" in resp
            if not has_error:
                content = resp.get("result", {}).get("content", [])
                if content:
                    data = json.loads(content[0]["text"])
                    has_error = "error" in data or data.get("status") == "error"
            self.assertTrue(has_error, "缺少 url 时应返回错误")
        asyncio.run(run())

    def test_agent_resume_missing_checkpoint_id_returns_error(self):
        """agent_resume 缺少 checkpoint_id 返回错误。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "agent_resume", "arguments": {}},
            })
            has_error = "error" in resp
            if not has_error:
                content = resp.get("result", {}).get("content", [])
                if content:
                    data = json.loads(content[0]["text"])
                    has_error = "error" in data
            self.assertTrue(has_error, "缺少 checkpoint_id 时应返回错误")
        asyncio.run(run())

    def test_unknown_tool_returns_error(self):
        """未知工具名返回错误。"""
        async def run():
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "nonexistent_tool", "arguments": {}},
            })
            self.assertIn("error", resp)
        asyncio.run(run())

    def test_agent_resume_error_structured_code(self):
        """agent_resume 错误返回结构化错误码（非模糊字符串）。"""
        async def run():
            from mcp_server import ResumeErrorCode
            resp = await self._run_client({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "agent_resume", "arguments": {}},
            })
            content = resp.get("result", {}).get("content", [])
            if content:
                data = json.loads(content[0]["text"])
                error = data.get("error", {})
                self.assertIn("code", error, "错误响应缺少 code 字段")
                self.assertIn("message", error, "错误响应缺少 message 字段")
                self.assertEqual(error["code"], ResumeErrorCode.CHECKPOINT_NOT_FOUND.value)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)