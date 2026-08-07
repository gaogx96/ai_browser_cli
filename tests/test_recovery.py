"""阶段 5 单元测试：错误分类、shadow 行为、active 恢复。

运行：
    python tests/test_recovery.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    AgentRunner,
    Decision,
    ErrorKind,
    PageSnapshot,
    RecoveryDecision,
    classify_error,
)


class MockBrowser:
    """mock 浏览器：可配置成功/失败行为。"""

    def __init__(self, fail_errors=None, nav_success=True):
        self.fail_errors = fail_errors or {}  # {action_type: error_str}
        self.nav_success = nav_success
        self.calls = []
        self.url = "about:blank"

    async def meta(self):
        return {"url": self.url, "title": "T", "interactiveCount": 1}

    async def tree(self):
        return "[@e1] link \"a\""

    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        if not self.nav_success:
            self.url = url  # 模拟 URL 已变化但动作失败
            return {"success": False, "error": "navigation timeout"}
        self.url = url
        return {"success": True}

    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
        err = self.fail_errors.get("click")
        if err:
            return {"success": False, "error": err}
        return {"success": True}

    async def type_text(self, target_id, text, **kw):
        self.calls.append(("type", target_id, text))
        return {"success": True}

    async def send_command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "evaluate":
            return {"result": "fp"}
        if action == "targets":
            return {"targets": [{"id": "t1", "type": "page", "url": self.url}]}
        return {"status": "ok"}


class FakeLLM:
    """mock LLM：返回固定决策序列。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = []

    async def decide(self, task, tree, meta, history, extra_context=None):
        self.calls.append(len(history))
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


def mk_browser(verify_mode="off", recovery_mode="off", context_mode="legacy", fail_errors=None, nav_success=True):
    os.environ["AGENT_VERIFY_MODE"] = verify_mode
    os.environ["AGENT_RECOVERY_MODE"] = recovery_mode
    os.environ["AGENT_CONTEXT_MODE"] = context_mode
    browser = MockBrowser(fail_errors=fail_errors, nav_success=nav_success)
    return browser


class TestClassifyError(unittest.TestCase):
    def test_stale(self):
        self.assertEqual(classify_error(None, {"error": "stale element"}), ErrorKind.STALE_TARGET)

    def test_not_found(self):
        self.assertEqual(classify_error(None, {"error": "element not found"}), ErrorKind.ELEMENT_NOT_FOUND)

    def test_timeout(self):
        self.assertEqual(classify_error(None, {"error": "navigation timeout"}), ErrorKind.NAVIGATION_TIMEOUT)

    def test_unknown(self):
        self.assertEqual(classify_error(None, {"error": "weird"}), ErrorKind.UNKNOWN)


class TestRecoveryDecision(unittest.TestCase):
    """直接测试 _recover 方法。"""

    def _runner(self, recovery_mode="shadow"):
        os.environ["AGENT_RECOVERY_MODE"] = recovery_mode
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        runner = AgentRunner(browser=MockBrowser(), llm=FakeLLM([]))
        return runner

    def test_stale_target_reobserve(self):
        runner = self._runner()
        d = Decision(action_type="click", target_id="e5")
        result = {"success": False, "error": "stale element reference"}
        rec = runner._recover(d, result, None, None)
        self.assertEqual(rec.kind, "reobserve")
        self.assertTrue(rec.force_reobserve)
        self.assertFalse(rec.retry_action)

    def test_element_not_found_reobserve(self):
        runner = self._runner()
        d = Decision(action_type="click", target_id="e5")
        result = {"success": False, "error": "element not found"}
        rec = runner._recover(d, result, None, None)
        self.assertEqual(rec.kind, "reobserve")
        self.assertTrue(rec.force_reobserve)

    def test_nav_timeout_url_changed_no_retry(self):
        runner = self._runner()
        d = Decision(action_type="navigate", url="https://example.com")
        result = {"success": False, "error": "navigation timeout"}
        after = PageSnapshot("t1", "https://example.com", "T", "fp", {}, (), True)
        rec = runner._recover(d, result, None, after)
        self.assertEqual(rec.kind, "none")
        self.assertFalse(rec.retry_action)

    def test_nav_timeout_url_unchanged_retry_once(self):
        runner = self._runner()
        d = Decision(action_type="navigate", url="https://example.com")
        result = {"success": False, "error": "navigation timeout"}
        after = PageSnapshot("t1", "about:blank", "", None, {}, (), True)
        rec = runner._recover(d, result, None, after)
        self.assertEqual(rec.kind, "retry")
        self.assertTrue(rec.retry_action)
        self.assertEqual(rec.max_attempts, 1)

    def test_nav_timeout_retry_exhausted(self):
        runner = self._runner()
        d = Decision(action_type="navigate", url="https://example.com")
        result = {"success": False, "error": "navigation timeout"}
        after = PageSnapshot("t1", "about:blank", "", None, {}, (), True)
        runner._recovery_retry_counts["navigate:https://example.com"] = 1  # 已重试一次
        rec = runner._recover(d, result, None, after)
        self.assertEqual(rec.kind, "none")

    def test_success_no_recovery(self):
        runner = self._runner()
        d = Decision(action_type="click", target_id="e5")
        result = {"success": True}
        rec = runner._recover(d, result, None, None)
        self.assertEqual(rec.kind, "none")

    def test_unknown_error_legacy_fallback(self):
        runner = self._runner()
        d = Decision(action_type="click", target_id="e5")
        result = {"success": False, "error": "some weird error"}
        rec = runner._recover(d, result, None, None)
        self.assertEqual(rec.kind, "none")


class TestShadowNoChange(unittest.TestCase):
    """shadow 模式：输出恢复建议，但不改变动作序列。"""

    def _run(self, recovery_mode, fail_errors):
        browser = mk_browser(verify_mode="off", recovery_mode=recovery_mode, fail_errors=fail_errors)
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        return result, browser.calls

    def test_shadow_reobserve_does_not_change_actions(self):
        """shadow 下 stale target 只记录，不改变动作序列（click 仍执行一次）。"""
        off_result, off_calls = self._run("off", {"click": "stale element"})
        shadow_result, shadow_calls = self._run("shadow", {"click": "stale element"})

        # off 和 shadow 的 click 调用次数应一致（shadow 不重放）
        off_clicks = [c for c in off_calls if c[0] == "click"]
        shadow_clicks = [c for c in shadow_calls if c[0] == "click"]
        self.assertEqual(len(off_clicks), len(shadow_clicks))

    def test_shadow_legacy_retry_3_times(self):
        """shadow 下未知错误仍走旧逻辑（重试 3 次后进入黑名单）。

        注意：_execute 的 click 分支对业务错误返回 {"success": True, "data": ...}，
        所以旧 _retry_counts 只对 _execute 抛出的异常生效。
        这里模拟异常场景来测试黑名单机制。
        """
        class ExceptionClickBrowser(MockBrowser):
            async def click(self, target_id, **kw):
                self.calls.append(("click", target_id))
                raise Exception("weird error")

        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "shadow"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        browser = ExceptionClickBrowser()
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},  # 第 4 次，应被黑名单拦截
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # 3 次失败后 target 进入黑名单
        self.assertIn("e5", runner.attempted)
        # 第 4 次 click 应被黑名单拦截（返回 {"success": False}）
        clicks = [c for c in browser.calls if c[0] == "click"]
        self.assertEqual(len(clicks), 3)


class TestActiveRecovery(unittest.TestCase):
    """active 模式：reobserve 不重放旧动作，回到 LLM 重新决策。"""

    def test_active_stale_target_reobserve_no_replay(self):
        """STALE_TARGET 在 active 下应跳过重放，让 LLM 重新决策。"""
        browser = mk_browser(verify_mode="off", recovery_mode="active", fail_errors={"click": "stale element"})
        decisions = [
            {"action": "click", "target_id": "e5"},  # 失败 → reobserve → continue
            {"action": "stop", "reason": "完成"},     # 重新决策直接 stop
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # click 只应执行一次（不重放）
        clicks = [c for c in browser.calls if c[0] == "click"]
        self.assertEqual(len(clicks), 1)
        # LLM 被调用 2 次（一次失败决策 + 一次重新决策）
        self.assertEqual(len(llm.calls), 2)

    def test_active_element_not_found_reobserve(self):
        browser = mk_browser(verify_mode="off", recovery_mode="active", fail_errors={"click": "element not found"})
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        clicks = [c for c in browser.calls if c[0] == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(len(llm.calls), 2)

    def test_active_nav_timeout_url_changed_no_retry(self):
        """导航超时但 URL 已变化 → 不重试，继续。

        需要 verify_mode=shadow 让 after snapshot 被采集，
        否则 _recover 无法判断 URL 是否变化。
        """
        browser = mk_browser(verify_mode="shadow", recovery_mode="active", nav_success=False)
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # navigate 只调用一次（URL 已变化，不重试）
        navs = [c for c in browser.calls if c[0] == "navigate"]
        self.assertEqual(len(navs), 1)

    def test_active_nav_timeout_url_unchanged_retry_once(self):
        """导航超时且 URL 未变化 → 重试一次。"""
        class NavFailBrowser(MockBrowser):
            async def navigate(self, url, **kw):
                self.calls.append(("navigate", url))
                # 第一次失败且 URL 不变，第二次成功
                if self.calls.count(("navigate", url)) == 1:
                    return {"success": False, "error": "navigation timeout"}
                self.url = url
                return {"success": True}

        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "active"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        browser = NavFailBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        navs = [c for c in browser.calls if c[0] == "navigate"]
        self.assertEqual(len(navs), 2)  # 原始 + 重试


if __name__ == "__main__":
    unittest.main(verbosity=2)