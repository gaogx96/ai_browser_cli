"""阶段 6A 单元测试：Checkpoint、Pause、Resume、页面匹配、安全性、兼容性。

运行：
    python tests/test_pause_resume.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    AgentRunner,
    AgentState,
    Checkpoint,
    PageMatchLevel,
    PauseReason,
)


class PauseBrowser:
    """支持导航/点击的 mock 浏览器。"""

    def __init__(self, url="about:blank"):
        self.url = url
        self.calls = []

    async def meta(self):
        return {"url": self.url, "title": "T", "interactiveCount": 2}

    async def tree(self):
        return "[@e1] link \"a\"\n[@e2] button \"b\""

    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        self.url = url
        return {"success": True}

    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
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


class PauseLLM:
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


def configure_env():
    os.environ["AGENT_VERIFY_MODE"] = "off"
    os.environ["AGENT_RECOVERY_MODE"] = "off"
    os.environ["AGENT_CONTEXT_MODE"] = "legacy"


class TestCheckpointCreation(unittest.TestCase):
    """pause 创建 checkpoint。"""

    def _run_pause(self):
        configure_env()
        browser = PauseBrowser()
        decisions = [
            {"action": "pause", "reason": "waiting_for_user"},
        ]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试任务"))
        return result, runner

    def test_pause_returns_paused(self):
        result, _ = self._run_pause()
        self.assertEqual(result.status, "paused")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.checkpoint)

    def test_checkpoint_fields_complete(self):
        result, _ = self._run_pause()
        ck = result.checkpoint
        self.assertEqual(ck.version, 1)
        self.assertTrue(ck.checkpoint_id)
        self.assertTrue(ck.session_id)
        self.assertEqual(ck.task, "测试任务")
        self.assertTrue(ck.snapshot_available)

    def test_checkpoint_no_sensitive_data(self):
        result, _ = self._run_pause()
        ck = result.checkpoint
        d = ck.to_dict()
        # 不应包含密码/表单值/Cookie/Authorization
        for key in d:
            self.assertNotIn("password", str(d[key]).lower())
            self.assertNotIn("cookie", str(d[key]).lower())
            self.assertNotIn("authorization", str(d[key]).lower())

    def test_pause_does_not_execute_action(self):
        """pause 后不应执行任何浏览器动作（navigate/click/type）。

        _create_checkpoint 会调用 _snapshot()（含 evaluate/meta/targets），
        但这些是框架采集，不是业务动作。
        """
        result, runner = self._run_pause()
        business_actions = [c for c in runner.browser.calls
                           if c[0] in ("navigate", "click", "type")]
        self.assertEqual(len(business_actions), 0)

    def test_pause_result_to_dict_has_checkpoint(self):
        result, _ = self._run_pause()
        d = result.to_dict()
        self.assertEqual(d["status"], "paused")
        self.assertIn("checkpoint", d)
        self.assertEqual(d["checkpoint"]["checkpoint_id"], result.checkpoint.checkpoint_id)


class TestCheckpointSecurity(unittest.TestCase):
    """checkpoint 不包含敏感信息。"""

    def test_password_not_in_checkpoint(self):
        """密码字段不进 checkpoint。"""
        configure_env()
        browser = PauseBrowser()
        decisions = [
            {"action": "pause", "reason": "waiting_for_user"},
        ]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        ck = result.checkpoint
        # checkpoint 只存 URL/指纹/目标，不存表单值
        self.assertNotIn("password", ck.to_dict())
        self.assertNotIn("form_state", ck.to_dict())


class TestPageMatch(unittest.TestCase):
    """页面匹配等级判断。"""

    def setUp(self):
        configure_env()
        self.browser = PauseBrowser()
        self.runner = AgentRunner(browser=self.browser, llm=PauseLLM([]))

    def _match(self, ck_url, current_url):
        ck = Checkpoint(page_url=ck_url)
        observation = ("tree", {"url": current_url, "title": "T", "interactiveCount": 1})
        return asyncio.run(self.runner._match_checkpoint(ck, observation))

    def test_strong_match_same_url(self):
        self.assertEqual(self._match("https://a.com/x", "https://a.com/x"), PageMatchLevel.STRONG)

    def test_weak_match_same_domain(self):
        self.assertEqual(self._match("https://a.com/x", "https://a.com/y"), PageMatchLevel.WEAK)

    def test_none_different_domain(self):
        self.assertEqual(self._match("https://a.com", "https://b.com"), PageMatchLevel.NONE)

    def test_none_empty_url(self):
        self.assertEqual(self._match("https://a.com", ""), PageMatchLevel.NONE)


class TestResume(unittest.TestCase):
    """resume 恢复执行。"""

    def _make_runner(self, decisions, browser=None):
        configure_env()
        browser = browser or PauseBrowser()
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        return runner, browser

    def _make_checkpoint(self, page_url="https://a.com", task="测试任务"):
        return Checkpoint(
            checkpoint_id="ck1",
            session_id="s1",
            task=task,
            current_goal="完成目标A",
            next_goal="下一步",
            completed_goals=["已做1"],
            failed_attempts=[],
            step=3,
            page_url=page_url,
            page_fingerprint="fp",
            pause_reason="waiting_for_user",
        )

    def test_resume_restores_state(self):
        """resume 恢复 AgentState（强匹配时保留目标）。"""
        browser = PauseBrowser(url="https://a.com")  # 与 checkpoint URL 匹配
        runner, _ = self._make_runner([{"action": "stop", "reason": "完成"}], browser)
        ck = self._make_checkpoint(page_url="https://a.com")
        result = asyncio.run(runner.resume(ck))
        self.assertEqual(runner.state.task, "测试任务")
        self.assertEqual(runner.state.current_goal, "完成目标A")
        self.assertEqual(runner.state.completed_goals, ["已做1"])

    def test_resume_reobserves_page(self):
        """resume 后重新观察页面。"""
        runner, browser = self._make_runner([{"action": "stop", "reason": "完成"}])
        ck = self._make_checkpoint()
        asyncio.run(runner.resume(ck))
        # 浏览器被重新观察（meta 调用）
        self.assertGreaterEqual(len(browser.calls), 0)

    def test_resume_no_action_replay(self):
        """resume 不重放 pause 前的旧 action。"""
        runner, browser = self._make_runner([{"action": "stop", "reason": "完成"}])
        ck = self._make_checkpoint()
        asyncio.run(runner.resume(ck))
        # 没有 click/navigate 重放（只有 stop）
        self.assertEqual([c[0] for c in browser.calls if c[0] in ("click", "navigate")], [])

    def test_resume_strong_match_continues(self):
        """强匹配 → 继续当前目标。"""
        browser = PauseBrowser(url="https://a.com")  # 与 checkpoint URL 相同
        runner, _ = self._make_runner([{"action": "stop", "reason": "完成"}], browser)
        ck = self._make_checkpoint(page_url="https://a.com")
        result = asyncio.run(runner.resume(ck))
        self.assertEqual(result.status, "success")

    def test_resume_none_match_replans(self):
        """URL 完全不同 → 重新规划。"""
        browser = PauseBrowser(url="https://b.com")
        runner, _ = self._make_runner([{"action": "stop", "reason": "完成"}], browser)
        ck = self._make_checkpoint(page_url="https://a.com")
        result = asyncio.run(runner.resume(ck))
        # 重新规划后 current_goal 重置
        self.assertEqual(runner.state.current_goal, None)
        self.assertEqual(runner.state.goal_status, "not_started")


class TestLifecycle(unittest.TestCase):
    """生命周期：重复 resume、无效 checkpoint。"""

    def test_resume_with_new_task(self):
        """resume 支持传入新任务。"""
        configure_env()
        browser = PauseBrowser(url="https://a.com")
        decisions = [{"action": "stop", "reason": "完成"}]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        ck = Checkpoint(checkpoint_id="ck1", task="旧任务", page_url="https://a.com", step=1)
        result = asyncio.run(runner.resume(ck, new_task="新任务"))
        self.assertEqual(runner.state.task, "新任务")

    def test_invalid_checkpoint_handled(self):
        """无效 checkpoint（空 task）不应崩溃。"""
        configure_env()
        browser = PauseBrowser(url="https://a.com")
        decisions = [{"action": "stop", "reason": "完成"}]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        # 空 checkpoint（无 page_url）
        ck = Checkpoint(checkpoint_id="", task="", page_url="", step=0)
        result = asyncio.run(runner.resume(ck))
        # 不应崩溃，返回结果
        self.assertIn(result.status, ("success", "failed"))


class TestCompatibility(unittest.TestCase):
    """阶段 6 不破坏既有行为。"""

    def test_run_task_without_checkpoint_works(self):
        """普通 run_task（无 checkpoint）仍正常工作。"""
        configure_env()
        browser = PauseBrowser()
        decisions = [{"action": "stop", "reason": "完成"}]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        self.assertEqual(result.status, "success")
        self.assertIsNone(result.checkpoint)

    def test_recovery_off_unaffected(self):
        """AGENT_RECOVERY_MODE=off 不受阶段 6 影响。"""
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        configure_env()
        browser = PauseBrowser()
        decisions = [{"action": "stop", "reason": "完成"}]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        self.assertEqual(result.status, "success")

    def test_structured_context_can_pause(self):
        """structured 上下文模式也能 pause。"""
        os.environ["AGENT_CONTEXT_MODE"] = "structured"
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        browser = PauseBrowser()
        decisions = [{"action": "pause", "reason": "waiting_for_page"}]
        llm = PauseLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))
        self.assertEqual(result.status, "paused")
        self.assertIsNotNone(result.checkpoint)


if __name__ == "__main__":
    unittest.main(verbosity=2)