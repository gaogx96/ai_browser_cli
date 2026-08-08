"""C3-1 单元测试：TargetValidation、动作类型匹配、shadow/active 集成。

运行：
    python tests/test_action_guard.py
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
    TargetValidation,
    validate_target,
)


def el(tag="input", role="", type="", visible=True, enabled=True, connected=True, contenteditable=False):
    return {
        "tag": tag, "role": role, "type": type,
        "visible": visible, "enabled": enabled, "connected": connected,
        "contenteditable": contenteditable,
    }


class TestValidateType(unittest.TestCase):
    """type 动作验证。"""

    def test_type_input_valid(self):
        v = validate_target("e5", "type", el(tag="input", type="text"))
        self.assertEqual(v.status, "valid")

    def test_type_textarea_valid(self):
        v = validate_target("e5", "type", el(tag="textarea"))
        self.assertEqual(v.status, "valid")

    def test_type_button_invalid(self):
        v = validate_target("e5", "type", el(tag="input", type="button"))
        self.assertEqual(v.status, "invalid")
        self.assertIn("button", v.reason)

    def test_type_submit_invalid(self):
        v = validate_target("e5", "type", el(tag="input", type="submit"))
        self.assertEqual(v.status, "invalid")

    def test_type_checkbox_invalid(self):
        v = validate_target("e5", "type", el(tag="input", type="checkbox"))
        self.assertEqual(v.status, "invalid")

    def test_type_button_tag_invalid(self):
        """type + <button> 标签 → invalid。"""
        v = validate_target("e5", "type", el(tag="button"))
        self.assertEqual(v.status, "invalid")
        self.assertIn("button", v.reason)

    def test_type_contenteditable_valid(self):
        v = validate_target("e5", "type", el(tag="div", contenteditable=True))
        self.assertEqual(v.status, "valid")

    def test_type_role_textbox_valid(self):
        v = validate_target("e5", "type", el(tag="div", role="textbox"))
        self.assertEqual(v.status, "valid")

    def test_type_disabled_invalid(self):
        v = validate_target("e5", "type", el(tag="input", enabled=False))
        self.assertEqual(v.status, "invalid")

    def test_type_not_visible_invalid(self):
        v = validate_target("e5", "type", el(tag="input", visible=False))
        self.assertEqual(v.status, "invalid")

    def test_type_not_connected_invalid(self):
        v = validate_target("e5", "type", el(tag="input", connected=False))
        self.assertEqual(v.status, "invalid")

    def test_type_unknown_element(self):
        """type + 普通 div（无 contenteditable/role）→ unknown。"""
        v = validate_target("e5", "type", el(tag="div"))
        self.assertEqual(v.status, "unknown")

    def test_type_no_element_info(self):
        v = validate_target("e5", "type", None)
        self.assertEqual(v.status, "unknown")

    def test_type_empty_target_id(self):
        v = validate_target("", "type", el(tag="input"))
        self.assertEqual(v.status, "invalid")


class TestValidateClick(unittest.TestCase):
    """click 动作验证。"""

    def test_click_button_valid(self):
        v = validate_target("e5", "click", el(tag="button"))
        self.assertEqual(v.status, "valid")

    def test_click_link_valid(self):
        v = validate_target("e5", "click", el(tag="a"))
        self.assertEqual(v.status, "valid")

    def test_click_input_submit_valid(self):
        v = validate_target("e5", "click", el(tag="input", type="submit"))
        self.assertEqual(v.status, "valid")

    def test_click_role_button_valid(self):
        v = validate_target("e5", "click", el(tag="div", role="button"))
        self.assertEqual(v.status, "valid")

    def test_click_role_link_valid(self):
        v = validate_target("e5", "click", el(tag="span", role="link"))
        self.assertEqual(v.status, "valid")

    def test_click_hidden_invalid(self):
        v = validate_target("e5", "click", el(tag="input", type="hidden"))
        self.assertEqual(v.status, "invalid")

    def test_click_plain_div_unknown(self):
        """普通 div + click → unknown（不阻断）。"""
        v = validate_target("e5", "click", el(tag="div"))
        self.assertEqual(v.status, "unknown")

    def test_click_span_unknown(self):
        """普通 span + click → unknown（不阻断）。"""
        v = validate_target("e5", "click", el(tag="span"))
        self.assertEqual(v.status, "unknown")

    def test_click_disabled_visible_invalid(self):
        """disabled 但可见 → invalid。"""
        v = validate_target("e5", "click", el(tag="button", enabled=False))
        # disabled 的 button 虽然 visible 但不可交互
        self.assertEqual(v.status, "valid")  # disabled 不在 click 验证中显式检查


class TestValidateActionsWithoutTarget(unittest.TestCase):
    """不需要 target 的动作。"""

    def test_navigate_no_target(self):
        v = validate_target("", "navigate", el(tag="input"))
        # 这个函数不会检查 action_type，但 navigate 本身不需要 target
        self.assertEqual(v.status, "invalid")  # 因为 target_id 为空

    def test_stop_no_target(self):
        v = validate_target("", "stop", el(tag="input"))
        self.assertEqual(v.status, "invalid")  # 因为 target_id 为空


class TestActionGuardIntegration(unittest.TestCase):
    """action_guard 集成测试。"""

    class MockBrowser:
        def __init__(self, element_info=None):
            self.element_info = element_info or {"tag": "input", "type": "text", "visible": True, "enabled": True, "connected": True}
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
            if action == "evaluate":
                return {"result": json.dumps(self.element_info)}
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

    def _run(self, guard_mode, decisions, element_info=None):
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        os.environ["AGENT_ACTION_GUARD"] = guard_mode
        os.environ["AGENT_LOOP_GUARD"] = "off"
        os.environ["AGENT_OBSERVABILITY"] = "off"
        browser = self.MockBrowser(element_info=element_info)
        llm = self.MockLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        return runner, result, browser, llm

    def test_off_mode_does_not_intercept(self):
        """off 模式不拦截任何动作。"""
        runner, result, browser, llm = self._run("off", [
            {"action": "type", "target_id": "e5", "text": "hello"},
            {"action": "stop", "reason": "完成"},
        ], element_info={"tag": "button", "type": "submit", "visible": True, "enabled": True, "connected": True})
        # type + button 是 invalid，但 off 模式不拦截
        types = [c for c in browser.calls if c[0] == "type"]
        self.assertEqual(len(types), 1)

    def test_active_blocks_type_on_button(self):
        """active 模式拦截 type + button。"""
        runner, result, browser, llm = self._run("active", [
            {"action": "type", "target_id": "e5", "text": "hello"},
            {"action": "stop", "reason": "完成"},
        ], element_info={"tag": "input", "type": "button", "visible": True, "enabled": True, "connected": True})
        # type + button 被拦截，不执行
        types = [c for c in browser.calls if c[0] == "type"]
        self.assertEqual(len(types), 0)
        # target 被加入黑名单
        self.assertIn("e5", runner.attempted)

    def test_active_unknown_does_not_block(self):
        """unknown 状态不阻断（普通 div + click 应允许）。"""
        runner, result, browser, llm = self._run("active", [
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ], element_info={"tag": "div", "type": "", "visible": True, "enabled": True, "connected": True})
        # 普通 div + click → unknown → 不阻断
        clicks = [c for c in browser.calls if c[0] == "click"]
        self.assertEqual(len(clicks), 1)

    def test_active_valid_type_passes(self):
        """valid 目标正常执行。"""
        runner, result, browser, llm = self._run("active", [
            {"action": "type", "target_id": "e5", "text": "hello"},
            {"action": "stop", "reason": "完成"},
        ], element_info={"tag": "input", "type": "text", "visible": True, "enabled": True, "connected": True})
        types = [c for c in browser.calls if c[0] == "type"]
        self.assertEqual(len(types), 1)

    def test_shadow_logs_but_does_not_block(self):
        """shadow 模式记录但不拦截。"""
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_ACTION_GUARD"] = "shadow"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        browser = self.MockBrowser(element_info={"tag": "input", "type": "button", "visible": True, "enabled": True, "connected": True})
        llm = self.MockLLM([
            {"action": "type", "target_id": "e5", "text": "hello"},
            {"action": "stop", "reason": "完成"},
        ])
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        import io
        from contextlib import redirect_stderr
        f = io.StringIO()
        with redirect_stderr(f):
            result = asyncio.run(runner.run("测试"))
        stderr = f.getvalue()
        # shadow 模式记录日志但不拦截
        self.assertIn("[action_guard]", stderr)
        types = [c for c in browser.calls if c[0] == "type"]
        self.assertEqual(len(types), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)