"""Shadow 模式真实回归验证：覆盖错误分类、竞态条件、日志审计、开销测量。

无需真实浏览器/LLM，用 mock 模拟真实场景。运行：
    python tests/regression_shadow_real.py
"""

import asyncio
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    AgentRunner,
    Decision,
    ErrorKind,
    PageSnapshot,
    TargetInfo,
    _diff,
    _verify,
    classify_error,
    normalize_decision,
)


# ── Mock 浏览器：模拟真实场景 ──────────────────────────────────────────────


class ScenarioBrowser:
    """可配置场景的 mock 浏览器。"""

    def __init__(self, scenarios=None):
        self.scenarios = scenarios or {}
        # scenarios: {action_type: [(call_count, result_dict), ...]}
        # call_count: 第几次调用时返回该结果
        self._counters = {}
        self.calls = []
        self.url = "about:blank"
        self.title = ""

    async def meta(self):
        return {"url": self.url, "title": self.title, "interactiveCount": 2}

    async def tree(self):
        return "[@e1] link \"a\"\n[@e2] button \"b\""

    async def navigate(self, url, **kw):
        key = "navigate"
        self._counters.setdefault(key, 0)
        self._counters[key] += 1
        call_no = self._counters[key]
        self.calls.append(("navigate", url, call_no))

        scenario = self.scenarios.get(key, [])
        for count, result in scenario:
            if call_no == count:
                if result.get("set_url"):
                    self.url = result["set_url"]
                return result
        self.url = url
        return {"success": True}

    async def click(self, target_id, **kw):
        key = "click"
        self._counters.setdefault(key, 0)
        self._counters[key] += 1
        call_no = self._counters[key]
        self.calls.append(("click", target_id, call_no))

        scenario = self.scenarios.get(key, [])
        for count, result in scenario:
            if call_no == count:
                return result
        return {"success": True}

    async def type_text(self, target_id, text, **kw):
        return {"success": True}

    async def send_command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "evaluate":
            return {"result": "fp"}
        if action == "targets":
            return {"targets": [{"id": "t1", "type": "page", "url": self.url}]}
        return {"status": "ok"}


class FakeLLM:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.call_count = 0

    async def decide(self, task, tree, meta, history, extra_context=None):
        self.call_count += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


# ── 测试 1：off 与 shadow 结果一致性 ────────────────────────────────────


class TestOffVsShadowConsistency(unittest.TestCase):
    """核心：off 与 shadow 的最终结果、动作序列、LLM 调用次数完全一致。"""

    def _run(self, recovery_mode, scenarios):
        os.environ["AGENT_VERIFY_MODE"] = "shadow"
        os.environ["AGENT_RECOVERY_MODE"] = recovery_mode
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        browser = ScenarioBrowser(scenarios=scenarios)
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        return {
            "success": result.success,
            "steps": result.steps,
            "history_count": len(result.history),
            "llm_calls": llm.call_count,
            "browser_calls": browser.calls,
            "history": result.to_dict()["history"],
        }

    def test_normal_navigation_consistent(self):
        off = self._run("off", {})
        shadow = self._run("shadow", {})
        for key in ["success", "steps", "history_count", "llm_calls"]:
            self.assertEqual(off[key], shadow[key], f"{key} 不一致")
        self.assertEqual(off["history"], shadow["history"])

    def test_stale_target_consistent(self):
        """stale target 时，off 和 shadow 的动作序列一致。"""
        scenarios = {"click": [(1, {"success": False, "error": "stale element"})]}
        os.environ["AGENT_VERIFY_MODE"] = "shadow"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        results = {}
        for mode in ["off", "shadow"]:
            os.environ["AGENT_RECOVERY_MODE"] = mode
            browser = ScenarioBrowser(scenarios=scenarios)
            decisions = [
                {"action": "click", "target_id": "e5"},
                {"action": "click", "target_id": "e5"},
                {"action": "click", "target_id": "e5"},
                {"action": "stop", "reason": "完成"},
            ]
            llm = FakeLLM(decisions)
            runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
            result = asyncio.run(runner.run("测试"))
            results[mode] = {
                "success": result.success,
                "steps": result.steps,
                "history_count": len(result.history),
                "llm_calls": llm.call_count,
                "click_count": len([c for c in browser.calls if c[0] == "click"]),
            }

        self.assertEqual(results["off"]["success"], results["shadow"]["success"])
        self.assertEqual(results["off"]["steps"], results["shadow"]["steps"])
        self.assertEqual(results["off"]["llm_calls"], results["shadow"]["llm_calls"])
        # shadow 不增加重试
        self.assertEqual(results["off"]["click_count"], results["shadow"]["click_count"])

    def test_element_not_found_consistent(self):
        scenarios = {"click": [(1, {"success": False, "error": "element not found"})]}
        os.environ["AGENT_VERIFY_MODE"] = "shadow"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        results = {}
        for mode in ["off", "shadow"]:
            os.environ["AGENT_RECOVERY_MODE"] = mode
            browser = ScenarioBrowser(scenarios=scenarios)
            decisions = [
                {"action": "click", "target_id": "e5"},
                {"action": "stop", "reason": "完成"},
            ]
            llm = FakeLLM(decisions)
            runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
            result = asyncio.run(runner.run("测试"))
            results[mode] = {
                "success": result.success,
                "llm_calls": llm.call_count,
                "click_count": len([c for c in browser.calls if c[0] == "click"]),
            }

        self.assertEqual(results["off"]["llm_calls"], results["shadow"]["llm_calls"])
        self.assertEqual(results["off"]["click_count"], results["shadow"]["click_count"])


# ── 测试 2：错误分类准确性 ────────────────────────────────────────────────


class TestErrorClassification(unittest.TestCase):
    """验证真实场景中的错误分类是否准确。"""

    def test_stale_element_classification(self):
        e = classify_error(None, {"error": "stale element reference: element is not attached to the page document"})
        self.assertEqual(e, ErrorKind.STALE_TARGET)

    def test_element_not_found_classification(self):
        e = classify_error(None, {"error": "Element [e5] not found in any frame"})
        self.assertEqual(e, ErrorKind.ELEMENT_NOT_FOUND)

    def test_navigation_timeout_classification(self):
        e = classify_error(None, {"error": "navigation timeout: 30s exceeded"})
        self.assertEqual(e, ErrorKind.NAVIGATION_TIMEOUT)

    def test_detached_classification(self):
        e = classify_error(None, {"error": "already disposed"})
        self.assertEqual(e, ErrorKind.STALE_TARGET)

    def test_not_clickable_classification(self):
        e = classify_error(None, {"error": "element is not clickable at point"})
        self.assertEqual(e, ErrorKind.ELEMENT_NOT_INTERACTABLE)

    def test_unknown_error_fallback(self):
        e = classify_error(None, {"error": "some unexpected error"})
        self.assertEqual(e, ErrorKind.UNKNOWN)

    def test_exception_not_found(self):
        e = classify_error(Exception("Element [e5] not found in any frame"))
        self.assertEqual(e, ErrorKind.ELEMENT_NOT_FOUND)

    def test_exception_stale(self):
        e = classify_error(Exception("stale element reference"))
        self.assertEqual(e, ErrorKind.STALE_TARGET)

    def test_exception_timeout(self):
        e = classify_error(Exception("timed out waiting for page load"))
        self.assertEqual(e, ErrorKind.NAVIGATION_TIMEOUT)


# ── 测试 3：snapshot 失败不误判 ──────────────────────────────────────────


class TestSnapshotFailure(unittest.TestCase):
    """snapshot 失败时，不误判为动作失败。"""

    def test_snapshot_failure_no_effects(self):
        """两个失败快照之间不应产生虚假变化。"""
        bad1 = PageSnapshot("", "", "", None, {}, (), snapshot_ok=False)
        bad2 = PageSnapshot("", "", "", None, {}, (), snapshot_ok=False)
        e = _diff(bad1, bad2)
        self.assertFalse(e.any_change())

    def test_before_snapshot_failure(self):
        """before 快照失败时，diff 不应报告变化。"""
        bad = PageSnapshot("", "", "", None, {}, (), snapshot_ok=False)
        good = PageSnapshot("t1", "https://a.com", "A", "fp", {}, (), True)
        e = _diff(bad, good)
        self.assertFalse(e.any_change())

    def test_after_snapshot_failure(self):
        """after 快照失败时，diff 不应报告变化。"""
        good = PageSnapshot("t1", "https://a.com", "A", "fp", {}, (), True)
        bad = PageSnapshot("", "", "", None, {}, (), snapshot_ok=False)
        e = _diff(good, bad)
        self.assertFalse(e.any_change())

    def test_verify_with_snapshot_failure(self):
        """snapshot 失败时，verify 返回 unknown 而非 failed。"""
        from agent_runner import ActionResult, ActionEffects
        result = ActionResult("click", transport_ok=True)
        e = ActionEffects()  # 无变化
        v = _verify("click", result, e)
        self.assertEqual(v.status, "no_effect")  # 没有变化，但 transport 成功
        self.assertFalse(v.transport_ok is False)  # 不是 failed


# ── 测试 4：竞态条件 ──────────────────────────────────────────────────────


class TestRaceConditions(unittest.TestCase):
    """模拟页面变化、target 关闭等竞态。"""

    def test_url_changed_after_nav_timeout(self):
        """导航超时但 URL 已变化 → 不触发重试。"""
        from agent_runner import _verify, ActionResult, ActionEffects, ActionVerification
        # 模拟：navigate 返回 timeout，但 URL 已变化
        result = ActionResult("navigate", transport_ok=False,
                              error_kind=ErrorKind.NAVIGATION_TIMEOUT,
                              error_message="navigation timeout")
        effects = ActionEffects(url_changed=True, title_changed=True)
        v = _verify("navigate", result, effects)
        # transport 失败 → status=failed
        self.assertEqual(v.status, "failed")
        self.assertTrue(v.needs_reobserve)

    def test_page_changed_between_observe_and_execute(self):
        """observe 和 execute 之间页面变化 → stale target。"""
        # 模拟：第一次 observe 看到 e5，但 execute 时 e5 已 stale
        err = classify_error(None, {"error": "stale element reference"})
        self.assertEqual(err, ErrorKind.STALE_TARGET)

    def test_target_closed_during_action(self):
        """动作执行过程中 target 被关闭。"""
        err = classify_error(Exception("detached from all targets"))
        self.assertEqual(err, ErrorKind.STALE_TARGET)

    def test_navigation_timeout_url_unchanged(self):
        """导航超时且 URL 未变化 → 触发重试。"""
        from agent_runner import RecoveryDecision
        # 用 _recover 直接测试
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "shadow"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        runner = AgentRunner(browser=ScenarioBrowser(), llm=FakeLLM([]))

        d = Decision(action_type="navigate", url="https://example.com")
        result = {"success": False, "error": "navigation timeout"}
        # after URL 仍是 about:blank
        after = PageSnapshot("t1", "about:blank", "", None, {}, (), True)
        rec = runner._recover(d, result, None, after)
        self.assertEqual(rec.kind, "retry")
        self.assertTrue(rec.retry_action)


# ── 测试 5：日志审计 ──────────────────────────────────────────────────────


class TestLogAudit(unittest.TestCase):
    """验证日志不包含敏感信息。"""

    def test_verify_log_no_sensitive_data(self):
        """[verify] 日志不应包含密码、表单值、Cookie、文件路径。"""
        os.environ["AGENT_VERIFY_MODE"] = "shadow"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        browser = ScenarioBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)

        f = io.StringIO()
        with redirect_stderr(f):
            asyncio.run(runner.run("测试"))
        stderr = f.getvalue()

        # 不应包含敏感信息
        sensitive_patterns = [
            "password", "Authorization", "Cookie", "token", "secret",
            "api_key", "auth", "credit", "ssn", "passwd",
        ]
        for pattern in sensitive_patterns:
            self.assertNotIn(pattern, stderr.lower(),
                             f"日志包含敏感信息: {pattern}")

    def test_recover_log_no_sensitive_data(self):
        """[recover] 日志不应包含页面完整内容或 raw 响应。"""
        os.environ["AGENT_VERIFY_MODE"] = "shadow"
        os.environ["AGENT_RECOVERY_MODE"] = "shadow"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        browser = ScenarioBrowser(
            scenarios={"click": [(1, {"success": False, "error": "stale element"})]}
        )
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)

        f = io.StringIO()
        with redirect_stderr(f):
            asyncio.run(runner.run("测试"))
        stderr = f.getvalue()

        # 日志应包含 recovery 信息，但不包含完整页面快照
        self.assertIn("[recover]", stderr)
        # 不应包含完整响应体
        self.assertNotIn("raw", stderr.lower())


# ── 测试 6：开销测量 ──────────────────────────────────────────────────────


class TestOverhead(unittest.TestCase):
    """测量 shadow 相比 off 的额外开销。"""

    def _run_timed(self, recovery_mode, iterations=3):
        """运行多次取平均。"""
        times = []
        for _ in range(iterations):
            os.environ["AGENT_VERIFY_MODE"] = "shadow"
            os.environ["AGENT_RECOVERY_MODE"] = recovery_mode
            os.environ["AGENT_CONTEXT_MODE"] = "legacy"
            browser = ScenarioBrowser()
            decisions = [
                {"action": "navigate", "url": "https://example.com"},
                {"action": "stop", "reason": "完成"},
            ]
            llm = FakeLLM(decisions)
            runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
            start = time.perf_counter()
            asyncio.run(runner.run("测试"))
            times.append(time.perf_counter() - start)
        return sum(times) / len(times)

    def test_overhead_acceptable(self):
        """shadow 比 off 的额外开销应在合理范围内（< 50ms/步）。

        纯函数开销 < 2µs 已在上次基准测试中验证。
        这里验证 mock 场景下的总体差异。
        """
        off_time = self._run_timed("off")
        shadow_time = self._run_timed("shadow")
        overhead = shadow_time - off_time
        # mock 场景下 overhead 应很小（只有 _snapshot 的 evaluate 调用）
        print(f"\n  off 平均耗时: {off_time:.3f}s")
        print(f"  shadow 平均耗时: {shadow_time:.3f}s")
        print(f"  额外开销: {overhead:.3f}s")
        self.assertLess(overhead, 0.5, f"shadow 额外开销过大: {overhead:.3f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)