"""C3-D 单元测试：成功动作事实、冗余动作检测、上下文反馈。

运行：
    python tests/test_c3d_redundant.py
"""

import asyncio
import os
import sys
import unittest
from contextlib import redirect_stderr
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    ActionEffects,
    ActionVerification,
    AgentRunner,
    AgentState,
    Decision,
)


class MockBrowser:
    def __init__(self):
        self.url = "about:blank"
        self.calls = []
        self.form_value = ""  # 模拟表单值，type 后变化

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
            expr = kwargs.get("expression", "")
            # 检测 set_value 操作（包含 dispatchEvent 和 value_set）
            if "value_set" in expr:
                # 从表达式中提取值（粗粒度：type 后 form_value 非空）
                self.form_value = "Rust"
                return {"result": '{"ok": true, "value_set": true}'}
            # 检测 form_state 采集
            if "els = document.querySelectorAll" in expr and "for (const el of els)" in expr:
                val = self.form_value or ""
                return {"result": '{"wd": {"kind": "text", "value": "' + val + '"}}'}
            # 检测焦点元素采集
            if "document.activeElement" in expr:
                return {"result": '{"tag": "input", "id": "kw", "name": "wd", "role": "textbox", "type": "text"}'}
            # 检测 DOM 指纹采集
            if "querySelectorAll" in expr:
                return {"result": "fp"}
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


class TestSuccessfulActionTracking(unittest.TestCase):
    """成功动作事实跟踪。"""

    def _run(self, decisions, verify_mode="shadow"):
        os.environ["AGENT_VERIFY_MODE"] = verify_mode
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "dual"
        os.environ["AGENT_GOAL_ASSESSMENT"] = "shadow"
        os.environ["AGENT_LOOP_GUARD"] = "off"
        os.environ["AGENT_ACTION_GUARD"] = "off"
        os.environ["AGENT_REDUNDANT_GUARD"] = "off"
        os.environ["AGENT_OBSERVABILITY"] = "off"
        browser = MockBrowser()
        llm = MockLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        return runner, result, browser, llm

    def test_type_success_tracks_form_changed(self):
        """type 成功后 last_successful_action 包含 form_changed。"""
        runner, _, _, _ = self._run([
            {"action": "type", "target_id": "e5", "text": "hello"},
            {"action": "stop", "reason": "完成"},
        ])
        la = runner.state.last_successful_action
        self.assertIsNotNone(la)
        self.assertEqual(la["action_type"], "type")
        self.assertIn("form_changed", la.get("effects", []))

    def test_context_contains_success_feedback(self):
        """build_context_text 包含最近成功动作反馈。"""
        runner, _, _, _ = self._run([
            {"action": "type", "target_id": "e5", "text": "hello"},
            {"action": "stop", "reason": "完成"},
        ])
        context = runner.state.build_context_text()
        self.assertIn("最近动作已成功", context)
        self.assertIn("type", context)

    def test_no_successful_action_no_context(self):
        """没有成功动作时不显示反馈。"""
        runner, _, _, _ = self._run([
            {"action": "stop", "reason": "完成"},
        ])
        context = runner.state.build_context_text()
        self.assertNotIn("最近动作已成功", context)

    def test_no_sensitive_data_in_context(self):
        """成功动作反馈不包含密码/表单值。"""
        runner, _, _, _ = self._run([
            {"action": "type", "target_id": "e5", "text": "secret"},
            {"action": "stop", "reason": "完成"},
        ])
        context = runner.state.build_context_text()
        self.assertNotIn("secret", context)
        self.assertNotIn("password", context.lower())


class TestRedundantActionGuard(unittest.TestCase):
    """冗余动作检测。"""

    def _make_verification(self, status="success") -> ActionVerification:
        return ActionVerification(
            transport_ok=True, page_responded=True, expected_effect_seen=True,
            status=status,
        )

    def test_same_action_not_redundant_on_first(self):
        """第一次成功动作不标记为冗余。"""
        os.environ["AGENT_REDUNDANT_GUARD"] = "active"
        runner = AgentRunner(browser=MockBrowser(), llm=MockLLM([]))
        d = Decision(action_type="type", target_id="e5", text="Rust")
        v = self._make_verification("success")
        self.assertFalse(runner._is_redundant_action(d, v))

    def test_same_action_redundant_on_second(self):
        """第二次相同动作标记为冗余。"""
        os.environ["AGENT_REDUNDANT_GUARD"] = "active"
        runner = AgentRunner(browser=MockBrowser(), llm=MockLLM([]))
        d = Decision(action_type="type", target_id="e5", text="Rust")
        v = self._make_verification("success")
        runner._is_redundant_action(d, v)  # 第一次，不冗余
        self.assertTrue(runner._is_redundant_action(d, v))  # 第二次，冗余

    def test_different_value_not_redundant(self):
        """不同输入值允许覆盖。"""
        os.environ["AGENT_REDUNDANT_GUARD"] = "active"
        runner = AgentRunner(browser=MockBrowser(), llm=MockLLM([]))
        d1 = Decision(action_type="type", target_id="e5", text="Rust")
        d2 = Decision(action_type="type", target_id="e5", text="Python")
        v = self._make_verification("success")
        runner._is_redundant_action(d1, v)  # 第一次 Rust
        self.assertFalse(runner._is_redundant_action(d2, v))  # Python 不同，不冗余

    def test_off_mode_no_intervention(self):
        """off 模式不检测冗余。"""
        os.environ["AGENT_REDUNDANT_GUARD"] = "off"
        runner = AgentRunner(browser=MockBrowser(), llm=MockLLM([]))
        d = Decision(action_type="type", target_id="e5", text="Rust")
        v = self._make_verification("success")
        runner._is_redundant_action(d, v)
        self.assertFalse(runner._is_redundant_action(d, v))  # off 模式不拦截

    def test_no_effect_not_redundant(self):
        """no_effect 不标记为冗余。"""
        os.environ["AGENT_REDUNDANT_GUARD"] = "active"
        runner = AgentRunner(browser=MockBrowser(), llm=MockLLM([]))
        d = Decision(action_type="type", target_id="e5", text="Rust")
        v = self._make_verification("no_effect")
        self.assertFalse(runner._is_redundant_action(d, v))  # no_effect 不标记


if __name__ == "__main__":
    unittest.main(verbosity=2)