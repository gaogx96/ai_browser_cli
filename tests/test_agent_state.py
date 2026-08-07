"""阶段 4 单元测试：AgentState 状态机、结构化上下文、双写兼容性。

运行：
    python tests/test_agent_state.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import AgentRunner, AgentState, Decision, normalize_decision


class MockBrowser:
    def __init__(self):
        self.calls = []

    async def meta(self):
        return {"url": "https://example.com", "title": "Example", "interactiveCount": 2}

    async def tree(self):
        return "[@e1] link \"a\"\n[@e2] button \"b\""

    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        return {"status": "ok"}

    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
        return {"status": "ok"}

    async def type_text(self, target_id, text, **kw):
        self.calls.append(("type", target_id, text))
        return {"status": "ok"}

    async def send_command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"result": "fp"}


class FakeLLM:
    """mock LLM：捕获 extra_context，返回固定决策。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = []
        self.last_extra_context = None

    async def decide(self, task, tree, meta, history, extra_context=None):
        self.calls.append(extra_context)
        self.last_extra_context = extra_context
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


class TestAgentState(unittest.TestCase):
    def test_initial_state(self):
        s = AgentState(task="测试任务")
        self.assertEqual(s.task, "测试任务")
        self.assertEqual(s.goal_status, "not_started")
        self.assertEqual(s.completed_goals, [])
        self.assertEqual(s.current_goal, None)

    def test_observe_updates_step_and_summary(self):
        s = AgentState(task="测试任务")
        s.observe("[@e1] link \"a\"", {"url": "https://x.com", "title": "X", "interactiveCount": 1})
        self.assertEqual(s.step, 1)
        self.assertIn("url=https://x.com", s.last_observation_summary)
        self.assertIn("title=X", s.last_observation_summary)
        self.assertIn("[@e1]", s.last_observation_summary)

    def test_observe_truncates_long_tree(self):
        s = AgentState(task="t")
        long_tree = "\n".join(f"[@e{i}] button \"b{i}\"" for i in range(20))
        s.observe(long_tree, {"url": "u", "title": "t", "interactiveCount": 20})
        # 摘要应截断到 8 行 + "…"
        self.assertIn("…", s.last_observation_summary)
        self.assertLessEqual(s.last_observation_summary.count("[@e"), 8)

    def test_record_goal_proposal_sets_current_goal(self):
        s = AgentState(task="t")
        d = Decision(action_type="click", target_id="e5", next_goal="点击登录")
        s.record_goal_proposal(d)
        self.assertEqual(s.current_goal, "点击登录")
        self.assertEqual(s.goal_status, "in_progress")

    def test_record_goal_proposal_keeps_current_on_later(self):
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal="目标A"))
        # 第二个提议更新 next_goal，但 current_goal 保持（避免覆盖进行中的目标）
        s.record_goal_proposal(Decision(action_type="click", target_id="e6", next_goal="目标B"))
        self.assertEqual(s.current_goal, "目标A")
        self.assertEqual(s.next_goal, "目标B")

    def test_record_failure_bounded(self):
        s = AgentState(task="t")
        for i in range(10):
            s.record_failure(Decision(action_type="click", target_id="e5"), f"错误{i}")
        # failed_attempts 应限制在 5 条
        self.assertEqual(len(s.failed_attempts), 5)

    def test_mark_goal_completed(self):
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal="目标A"))
        s.mark_goal_completed()
        self.assertIn("目标A", s.completed_goals)
        self.assertEqual(s.current_goal, None)
        self.assertEqual(s.goal_status, "completed")

    def test_mark_goal_completed_no_duplicate(self):
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal="目标A"))
        s.mark_goal_completed()
        s.mark_goal_completed()
        self.assertEqual(len(s.completed_goals), 1)

    def test_build_context_structure(self):
        s = AgentState(task="t")
        s.observe("[@e1] link \"a\"", {"url": "u", "title": "t", "interactiveCount": 1})
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal="目标A"))
        ctx = s.build_context()
        self.assertEqual(ctx["task"], "t")
        self.assertEqual(ctx["current_goal"], "目标A")
        self.assertIn("last_observation", ctx)
        self.assertIsInstance(ctx["completed_goals"], list)

    def test_build_context_text_includes_goals(self):
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal="目标A"))
        text = s.build_context_text()
        self.assertIn("任务: t", text)
        self.assertIn("当前目标: 目标A", text)
        self.assertIn("下一目标: 目标A", text)


class TestContextModeDual(unittest.TestCase):
    """双写模式：AgentState 接入，同时保留 legacy history。"""

    def _run_dual(self):
        decisions = [
            {"action": "navigate", "url": "https://example.com", "next_goal": "打开页面"},
            {"action": "stop", "reason": "完成"},
        ]
        os.environ["AGENT_CONTEXT_MODE"] = "dual"
        os.environ["AGENT_VERIFY_MODE"] = "off"
        browser = MockBrowser()
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试任务"))
        return runner, result, llm

    def test_dual_passes_extra_context(self):
        runner, result, llm = self._run_dual()
        # dual 模式应传 extra_context
        self.assertIsNotNone(llm.last_extra_context)
        self.assertIn("任务: 测试任务", llm.last_extra_context)

    def test_dual_keeps_legacy_history(self):
        runner, result, llm = self._run_dual()
        # legacy history 仍保留
        self.assertGreater(len(result.history), 0)
        self.assertIn("action", result.history[0])
        self.assertEqual(result.history[0]["action"]["action"], "navigate")

    def test_dual_success_matches_legacy(self):
        runner, result, llm = self._run_dual()
        self.assertTrue(result.success)

    def test_state_completed_on_stop(self):
        runner, result, llm = self._run_dual()
        self.assertEqual(runner.state.goal_status, "completed")
        self.assertIn("打开页面", runner.state.completed_goals)


class TestContextModeLegacy(unittest.TestCase):
    """legacy 模式：保持旧行为，不传 extra_context，不创建 AgentState。"""

    def test_legacy_no_extra_context(self):
        decisions = [{"action": "stop", "reason": "完成"}]
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        os.environ["AGENT_VERIFY_MODE"] = "off"
        browser = MockBrowser()
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试任务"))
        self.assertIsNone(llm.last_extra_context)
        self.assertTrue(result.success)


class TestContextModeStructured(unittest.TestCase):
    """structured 模式：用 AgentState 上下文，不依赖 legacy history。"""

    def test_structured_passes_context(self):
        decisions = [{"action": "stop", "reason": "完成"}]
        os.environ["AGENT_CONTEXT_MODE"] = "structured"
        os.environ["AGENT_VERIFY_MODE"] = "off"
        browser = MockBrowser()
        llm = FakeLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试任务"))
        self.assertIsNotNone(llm.last_extra_context)
        self.assertTrue(result.success)


class TestNormalizeNewFormat(unittest.TestCase):
    def test_nested_action_format(self):
        d = normalize_decision({"action": {"type": "click", "target_id": "e5"}})
        self.assertEqual(d.action_type, "click")
        self.assertEqual(d.target_id, "e5")

    def test_structured_fields_preserved(self):
        d = normalize_decision(
            {
                "evaluation_previous_goal": "已完成",
                "next_goal": "下一步",
                "action": "click",
                "target_id": "e5",
            }
        )
        self.assertEqual(d.evaluation_previous_goal, "已完成")
        self.assertEqual(d.next_goal, "下一步")


if __name__ == "__main__":
    unittest.main(verbosity=2)