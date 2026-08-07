"""Active canary 测试：验证 active 恢复真正改变循环后不引入问题。

覆盖：
- STALE_TARGET / ELEMENT_NOT_FOUND 不重放旧动作，回到 LLM 重新决策
- NAVIGATION_TIMEOUT URL 已变化不重试；URL 未变化重试一次
- action_id 计数隔离
- 其他错误保持旧逻辑
- snapshot 失败不误触发 recovery
- 恢复不会无限循环
- 默认 AGENT_RECOVERY_MODE=off 行为不变

运行：
    python tests/test_recovery_canary.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import AgentRunner, ErrorKind, PageSnapshot, classify_error


class CanaryBrowser:
    """可配置的 active 场景 mock 浏览器。"""

    def __init__(self, click_errors=None, nav_errors=None):
        # click_errors: {target_id: error_str} — 该 target 每次点击都失败
        # nav_errors: 每次 navigate 失败的次数
        self.click_errors = click_errors or {}
        self.nav_fail_remaining = nav_errors or 0
        self.calls = []
        self.url = "about:blank"
        self.title = ""

    async def meta(self):
        return {"url": self.url, "title": self.title, "interactiveCount": 2}

    async def tree(self):
        return "[@e1] link \"a\"\n[@e2] button \"b\"\n[@e3] button \"c\""

    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        if self.nav_fail_remaining > 0:
            self.nav_fail_remaining -= 1
            return {"success": False, "error": "navigation timeout"}
        self.url = url
        return {"success": True}

    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
        err = self.click_errors.get(target_id)
        if err:
            return {"success": False, "error": err}
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


class CanaryLLM:
    """可编程 fake LLM：按决策序列返回，记录调用次数和传入的 history。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.call_count = 0
        self.history_sizes = []

    async def decide(self, task, tree, meta, history, extra_context=None):
        self.call_count += 1
        self.history_sizes.append(len(history))
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


def configure_env(recovery_mode="active", browser=None, llm=None):
    os.environ["AGENT_VERIFY_MODE"] = "shadow"
    os.environ["AGENT_RECOVERY_MODE"] = recovery_mode
    os.environ["AGENT_CONTEXT_MODE"] = "legacy"
    return browser, llm


class TestStaleTargetActive(unittest.TestCase):
    """STALE_TARGET 在 active 下：不重放旧动作，回到 LLM。"""

    def test_stale_target_no_replay_back_to_llm(self):
        """旧 target 只执行一次，reobserve 后 LLM 重新决策。"""
        browser = CanaryBrowser(click_errors={"e5": "stale element reference"})
        # 决策序列：第一次点 e5（失败→reobserve），第二次重新决策点 e6（成功）
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e6"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))

        # 旧 target e5 只执行一次（不重放）
        e5_clicks = [c for c in browser.calls if c[0] == "click" and c[1] == "e5"]
        self.assertEqual(len(e5_clicks), 1)
        # LLM 被调用至少 2 次（一次失败决策 + 一次 reobserve 后重新决策）
        self.assertGreaterEqual(llm.call_count, 2)
        # 新 target e6 被使用
        e6_clicks = [c for c in browser.calls if c[0] == "click" and c[1] == "e6"]
        self.assertGreaterEqual(len(e6_clicks), 1)

    def test_stale_target_reobserve_returns_to_llm(self):
        """reobserve 后确实重新走 observe→decide（LLM 看到新 history）。"""
        browser = CanaryBrowser(click_errors={"e5": "stale element reference"})
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # 第二次 LLM 调用时 history 已有 1 条（第一次失败的 click）
        self.assertGreaterEqual(llm.call_count, 2)
        self.assertGreaterEqual(llm.history_sizes[-1], 1)


class TestElementNotFoundActive(unittest.TestCase):
    """ELEMENT_NOT_FOUND 在 active 下：不重放旧动作，回到 LLM。"""

    def test_element_not_found_no_replay(self):
        browser = CanaryBrowser(click_errors={"e5": "element not found in any frame"})
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e6"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        e5_clicks = [c for c in browser.calls if c[0] == "click" and c[1] == "e5"]
        self.assertEqual(len(e5_clicks), 1)
        self.assertGreaterEqual(llm.call_count, 2)


class TestNavTimeoutActive(unittest.TestCase):
    """NAVIGATION_TIMEOUT 在 active 下。"""

    def test_url_changed_no_retry(self):
        """导航超时但 URL 已变化 → 不重试。"""
        class UrlChangedBrowser(CanaryBrowser):
            async def navigate(self, url, **kw):
                self.calls.append(("navigate", url))
                self.url = url  # 即使失败 URL 也变化
                return {"success": False, "error": "navigation timeout"}

        browser = UrlChangedBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        navs = [c for c in browser.calls if c[0] == "navigate"]
        self.assertEqual(len(navs), 1)  # 不重试

    def test_url_unchanged_retry_once_then_success(self):
        """导航超时且 URL 未变化 → 重试一次，第二次成功。"""
        browser = CanaryBrowser(nav_errors=1)  # 第一次失败，第二次成功
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        navs = [c for c in browser.calls if c[0] == "navigate"]
        self.assertEqual(len(navs), 2)  # 原始 + 重试
        self.assertTrue(result.success)

    def test_action_id_isolation(self):
        """不同导航动作的计数互不污染。"""
        class TwoNavBrowser(CanaryBrowser):
            def __init__(self):
                super().__init__()
                self.fail_urls = {"https://a.com": 1, "https://b.com": 1}

            async def navigate(self, url, **kw):
                self.calls.append(("navigate", url))
                if self.fail_urls.get(url, 0) > 0:
                    self.fail_urls[url] -= 1
                    # 失败时不设置 self.url，保持 about:blank
                    return {"success": False, "error": "navigation timeout"}
                self.url = url
                return {"success": True}

        browser = TwoNavBrowser()
        decisions = [
            {"action": "navigate", "url": "https://a.com"},
            {"action": "navigate", "url": "https://b.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # 两个导航各自重试一次
        a_navs = [c for c in browser.calls if c[0] == "navigate" and c[1] == "https://a.com"]
        b_navs = [c for c in browser.calls if c[0] == "navigate" and c[1] == "https://b.com"]
        self.assertEqual(len(a_navs), 2)  # a 重试一次
        self.assertEqual(len(b_navs), 2)  # b 重试一次（不受 a 影响）

    def test_retry_exhausted_normal_failure(self):
        """重试用尽后正常失败（不无限重试）。"""
        browser = CanaryBrowser(nav_errors=5)  # 持续失败
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        navs = [c for c in browser.calls if c[0] == "navigate"]
        # 最多重试一次（原始 + 1 次重试），不无限重试
        self.assertLessEqual(len(navs), 2)


class TestOtherErrorsLegacy(unittest.TestCase):
    """其他错误保持旧逻辑（3 次重试 + 黑名单）。"""

    def test_unknown_error_legacy_retry(self):
        """UNKNOWN 错误不被 active recovery 接管。"""
        class UnknownFailBrowser(CanaryBrowser):
            async def click(self, target_id, **kw):
                self.calls.append(("click", target_id))
                raise Exception("some weird unexpected error")

        browser = UnknownFailBrowser()
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},  # 第 4 次被黑名单拦截
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # 旧逻辑：3 次失败后黑名单
        self.assertIn("e5", runner.attempted)
        clicks = [c for c in browser.calls if c[0] == "click"]
        self.assertEqual(len(clicks), 3)  # 第 4 次被黑名单拦截

    def test_not_interactable_legacy(self):
        """ELEMENT_NOT_INTERACTABLE 走旧逻辑（不是新 recovery）。"""
        browser = CanaryBrowser(click_errors={"e5": "element is not clickable at point"})
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        # 不重放（旧逻辑：只在 LLM 再次决策时重试），LLM 只调用 2 次
        self.assertEqual(llm.call_count, 2)


class TestSnapshotFailureNoRecovery(unittest.TestCase):
    """snapshot 失败不触发 recovery。"""

    def test_snapshot_failure_not_recovery(self):
        """动作 transport 成功，但 after snapshot 失败 → 不触发 recovery。"""
        browser = CanaryBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        # 模拟 snapshot 失败：不改变 _recovery 逻辑，但验证 verify 不误判
        # 这里直接测 _verify 在无变化时返回 no_effect 而非 failed
        from agent_runner import ActionResult, ActionEffects, _verify
        v = _verify("navigate", ActionResult("navigate", transport_ok=True), ActionEffects())
        self.assertEqual(v.status, "no_effect")
        self.assertNotEqual(v.status, "failed")


class TestNoInfiniteLoop(unittest.TestCase):
    """恢复不会无限循环。"""

    def test_recovery_bounded_by_max_steps(self):
        """即使持续 stale，也受 max_steps 限制终止。"""
        browser = CanaryBrowser(click_errors={"e5": "stale element reference"})
        # LLM 一直返回 e5（持续失败），但受 max_steps 限制
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=5)
        result = asyncio.run(runner.run("测试"))
        # 即使持续失败，最终会 stop 或达到 max_steps
        self.assertLessEqual(result.steps, 5)


class TestDefaultOff(unittest.TestCase):
    """默认 AGENT_RECOVERY_MODE=off 行为不变。"""

    def test_off_no_recovery_log(self):
        """off 模式不产生 [recover] 日志，完全保留旧行为。

        注意：_execute 对业务错误返回 {"success": True, "data": ...}，
        旧 _retry_counts 只对异常生效。这里用异常模拟旧逻辑。
        """
        import io
        from contextlib import redirect_stderr

        class ExceptionBrowser(CanaryBrowser):
            async def click(self, target_id, **kw):
                self.calls.append(("click", target_id))
                raise Exception("stale element reference")

        browser = ExceptionBrowser()
        decisions = [
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "click", "target_id": "e5"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = CanaryLLM(decisions)
        configure_env(recovery_mode="off", browser=browser, llm=llm)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)

        f = io.StringIO()
        with redirect_stderr(f):
            result = asyncio.run(runner.run("测试"))
        stderr = f.getvalue()
        # off 模式不产生 [recover] 日志
        self.assertNotIn("[recover]", stderr)
        # 旧逻辑：3 次异常失败后黑名单
        self.assertIn("e5", runner.attempted)


if __name__ == "__main__":
    unittest.main(verbosity=2)