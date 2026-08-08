"""C3-2 单元测试：结构化 evaluate、语法防护、raw evaluate 开关。

运行：
    python tests/test_evaluate_structured.py
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    AgentRunner,
    Decision,
    EvaluateRequest,
    generate_script,
    normalize_evaluate_operation,
    _js_quote,
    STRUCTURED_EVALUATE_ACTIONS,
)


class TestJsQuote(unittest.TestCase):
    """JS 字符串安全转义。"""

    def test_simple_string(self):
        self.assertEqual(_js_quote("hello"), '"hello"')

    def test_quotes(self):
        q = _js_quote('he"llo')
        self.assertIn("\\", q)  # 引号被转义

    def test_newline(self):
        q = _js_quote("he\nllo")
        self.assertIn("\\n", q)

    def test_unicode(self):
        q = _js_quote("中文")
        self.assertIn("中文", q)  # ensure_ascii=False


class TestGenerateScript(unittest.TestCase):
    """结构化操作 → 固定脚本生成。"""

    def test_focus_script(self):
        req = EvaluateRequest(operation="focus", target_id="e5")
        script = generate_script(req)
        self.assertIn('data-agent-id="e5"', script)
        self.assertIn("el.focus()", script)

    def test_set_value_script(self):
        req = EvaluateRequest(operation="set_value", target_id="e5", value="Rust")
        script = generate_script(req)
        self.assertIn('data-agent-id="e5"', script)
        self.assertIn("dispatchEvent", script)
        self.assertIn("input", script)
        self.assertIn("change", script)

    def test_set_value_with_quotes(self):
        req = EvaluateRequest(operation="set_value", target_id="e5", value='he"llo')
        script = generate_script(req)
        # 引号应被正确转义，不会破坏脚本语法
        self.assertIn("he", script)

    def test_set_value_with_newline(self):
        req = EvaluateRequest(operation="set_value", target_id="e5", value="line1\nline2")
        script = generate_script(req)
        # 换行应被正确转义
        self.assertIn("line1", script)

    def test_scroll_script(self):
        req = EvaluateRequest(operation="scroll_into_view", target_id="e5")
        script = generate_script(req)
        self.assertIn("scrollIntoView", script)

    def test_read_property_script(self):
        req = EvaluateRequest(operation="read_property", target_id="e5", property_name="value")
        script = generate_script(req)
        self.assertIn("el[", script)
        self.assertIn("value", script)

    def test_unknown_operation(self):
        req = EvaluateRequest(operation="unknown_op")
        script = generate_script(req)
        self.assertIn("unknown_operation", script)

    def test_script_not_empty(self):
        """所有结构化操作都生成非空脚本。"""
        for op in ["focus", "set_value", "scroll_into_view", "dispatch_input", "dispatch_change", "read_property"]:
            req = EvaluateRequest(operation=op, target_id="e5", value="test", property_name="value")
            script = generate_script(req)
            self.assertTrue(len(script) > 50, f"{op} 脚本太短")


class TestNormalizeEvaluate(unittest.TestCase):
    """evaluate 操作提取。"""

    def test_operation_in_action(self):
        req = normalize_evaluate_operation({"operation": "focus", "target_id": "e5"})
        self.assertIsNotNone(req)
        self.assertEqual(req.operation, "focus")
        self.assertEqual(req.target_id, "e5")

    def test_no_operation_returns_none(self):
        req = normalize_evaluate_operation({"expression": "document.title"})
        self.assertIsNone(req)

    def test_set_value_operation(self):
        req = normalize_evaluate_operation({"operation": "set_value", "target_id": "e5", "value": "Rust"})
        self.assertIsNotNone(req)
        self.assertEqual(req.value, "Rust")


class TestStructuredActions(unittest.TestCase):
    """结构化操作作为 action 直接输出。"""

    def test_structured_actions_defined(self):
        self.assertIn("focus", STRUCTURED_EVALUATE_ACTIONS)
        self.assertIn("set_value", STRUCTURED_EVALUATE_ACTIONS)
        self.assertIn("scroll_into_view", STRUCTURED_EVALUATE_ACTIONS)
        self.assertIn("read_property", STRUCTURED_EVALUATE_ACTIONS)

    def test_structured_action_count(self):
        self.assertGreaterEqual(len(STRUCTURED_EVALUATE_ACTIONS), 6)


class MockBrowser:
    def __init__(self):
        self.url = "about:blank"
        self.calls = []

    async def meta(self):
        return {"url": self.url, "title": "T", "interactiveCount": 2}
    async def tree(self):
        return "[@e1] link \"a\""
    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        self.url = url
        return {"success": True}
    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
        return {"success": True}
    async def type_text(self, target_id, text, **kw):
        self.calls.append(("type", target_id, text))
        return {"success": True}
    async def send_command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "evaluate":
            return {"result": '{"ok": true}'}
        return {"status": "ok"}


class MockLLM:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.call_count = 0
    async def decide(self, task, tree, meta, history, extra_context=None):
        self.call_count += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


class TestRawEvaluateGuard(unittest.TestCase):
    """AGENT_RAW_EVALUATE 开关。"""

    def _run(self, raw_mode, action_type="evaluate", extra=None):
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        os.environ["AGENT_RAW_EVALUATE"] = raw_mode
        os.environ["AGENT_ACTION_GUARD"] = "off"
        os.environ["AGENT_LOOP_GUARD"] = "off"
        os.environ["AGENT_OBSERVABILITY"] = "off"
        browser = MockBrowser()
        action = {"action": action_type}
        if extra:
            action.update(extra)
        decisions = [action, {"action": "stop", "reason": "完成"}]
        llm = MockLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        return runner, result, browser, llm

    def test_off_blocks_raw_evaluate(self):
        """off 模式拒绝 raw evaluate（不发送到浏览器）。"""
        runner, result, browser, llm = self._run("off", extra={"expression": "document.title"})
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(len(eval_calls), 0)  # 被拒绝，不发送到浏览器
        # 任务可能成功（LLM 在 evaluate 被拒后输出 stop）

    def test_off_allows_structured_operation(self):
        """off 模式允许结构化操作（通过 evaluate 参数传入）。"""
        runner, result, browser, llm = self._run("off", extra={"operation": "focus", "target_id": "e5"})
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(len(eval_calls), 1)  # 结构化操作放行

    def test_off_allows_structured_action(self):
        """off 模式允许结构化操作（直接作为 action）。"""
        runner, result, browser, llm = self._run("off", action_type="focus", extra={"target_id": "e5"})
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(len(eval_calls), 1)  # 结构化操作放行

    def test_shadow_logs_raw_evaluate(self):
        """shadow 模式记录 raw evaluate 但不拦截。"""
        runner, result, browser, llm = self._run("shadow", extra={"expression": "document.title"})
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(len(eval_calls), 1)  # 放行

    def test_active_allows_raw_evaluate(self):
        """active 模式允许 raw evaluate。"""
        runner, result, browser, llm = self._run("active", extra={"expression": "document.title"})
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(len(eval_calls), 1)  # 放行


class TestStructuredActionIntegration(unittest.TestCase):
    """结构化操作在完整循环中工作。"""

    def test_focus_action_works(self):
        """focus 结构化操作在 _execute 中正常工作。"""
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        os.environ["AGENT_RAW_EVALUATE"] = "off"
        os.environ["AGENT_ACTION_GUARD"] = "off"
        os.environ["AGENT_LOOP_GUARD"] = "off"
        os.environ["AGENT_OBSERVABILITY"] = "off"
        browser = MockBrowser()
        llm = MockLLM([
            {"action": "focus", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ])
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # focus 在 _execute 中被转换为 evaluate
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(len(eval_calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)