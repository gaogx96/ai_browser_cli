"""C2 前置阻塞项修复测试：current_goal 推进、循环防护、click 焦点证据。

运行：
    python tests/test_c2_prereqs.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    ActionEffects,
    AgentRunner,
    AgentState,
    Decision,
    PageSnapshot,
    TargetInfo,
    assess_from_rules,
)


# ── 修复 1：current_goal 推进 ────────────────────────────────────────────


class TestCurrentGoalProgression(unittest.TestCase):
    def test_first_next_goal_sets_current(self):
        """首次 next_goal → 设置 current_goal。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="https://a.com", next_goal="打开百度首页"))
        self.assertEqual(s.current_goal, "打开百度首页")
        self.assertEqual(s.goal_status, "in_progress")

    def test_same_next_goal_no_duplicate(self):
        """相同 next_goal → 不重复推进。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="u", next_goal="打开百度首页"))
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal="打开百度首页"))
        self.assertEqual(s.current_goal, "打开百度首页")
        # 不应产生重复 transition
        self.assertEqual(len(s.goal_transitions), 1)

    def test_previous_completed_promotes_next(self):
        """LLM 表示上一目标已完成 → 晋升 next_goal。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="u", next_goal="打开百度首页"))
        s.record_goal_proposal(Decision(
            action_type="click", target_id="e5",
            next_goal="点击搜索框",
            evaluation_previous_goal="已完成，成功打开百度首页",
        ))
        self.assertEqual(s.previous_goal, "打开百度首页")
        self.assertEqual(s.current_goal, "点击搜索框")

    def test_previous_not_completed_keeps_current(self):
        """LLM 未说明完成 → 保留 current_goal，暂存 next_goal。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="u", next_goal="打开百度首页"))
        s.record_goal_proposal(Decision(
            action_type="click", target_id="e5",
            next_goal="点击搜索框",  # 没有 evaluation_previous_goal
        ))
        self.assertEqual(s.current_goal, "打开百度首页")
        self.assertEqual(s.next_goal, "点击搜索框")

    def test_llm_abnormal_output_no_corruption(self):
        """LLM 输出异常 → 不破坏 current_goal。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="u", next_goal="打开百度首页"))
        s.record_goal_proposal(Decision(action_type="click", target_id="e5", next_goal=""))
        self.assertEqual(s.current_goal, "打开百度首页")

    def test_completed_goals_not_auto_incremented(self):
        """普通 proposal 不自动增加 completed_goals。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="u", next_goal="打开百度首页"))
        s.record_goal_proposal(Decision(
            action_type="click", target_id="e5",
            next_goal="点击搜索框",
            evaluation_previous_goal="已完成",
        ))
        # 推进了 current_goal，但 completed_goals 不自动增加
        self.assertEqual(s.completed_goals, [])

    def test_goal_transitions_recorded(self):
        """目标推进记录 goal_transitions。"""
        s = AgentState(task="t")
        s.record_goal_proposal(Decision(action_type="navigate", url="u", next_goal="目标A"))
        s.record_goal_proposal(Decision(
            action_type="click", target_id="e5",
            next_goal="目标B",
            evaluation_previous_goal="已完成",
        ))
        self.assertEqual(len(s.goal_transitions), 2)
        self.assertEqual(s.goal_transitions[1]["from"], "目标A")
        self.assertEqual(s.goal_transitions[1]["to"], "目标B")


# ── 修复 2：循环防护（重复 no_effect 动作） ──────────────────────────────


class MockBrowser:
    def __init__(self, effect_on_click=False):
        self.url = "about:blank"
        self.calls = []
        self.effect_on_click = effect_on_click

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
        if action == "evaluate":
            return {"result": "fp"}
        if action == "targets":
            return {"targets": [{"id": "t1", "type": "page", "url": self.url}]}
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


class TestLoopGuard(unittest.TestCase):
    def _make_runner(self, loop_mode="active", decisions=None):
        os.environ["AGENT_VERIFY_MODE"] = "shadow"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        os.environ["AGENT_GOAL_ASSESSMENT"] = "shadow"
        os.environ["AGENT_LOOP_GUARD"] = loop_mode
        browser = MockBrowser()
        llm = MockLLM(decisions or [{"action": "stop", "reason": "完成"}])
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        return runner, browser

    def test_off_mode_no_intervention(self):
        """off 模式不干预。"""
        runner, _ = self._make_runner("off")
        v = type("V", (), {"status": "no_effect"})()
        rec = runner._handle_loop_guard(
            Decision(action_type="click", target_id="e5"),
            v, PageSnapshot("t1", "u", "t", "fp", {}, None, (), True))
        self.assertIsNone(rec)

    def test_first_no_effect_no_intervention(self):
        """第 1 次 no_effect → 不干预。"""
        runner, _ = self._make_runner("active")
        v = type("V", (), {"status": "no_effect"})()
        rec = runner._handle_loop_guard(
            Decision(action_type="click", target_id="e5"),
            v, PageSnapshot("t1", "u", "t", "fp", {}, None, (), True))
        self.assertIsNone(rec)

    def test_second_no_effect_reobserve(self):
        """第 2 次相同 no_effect → reobserve。"""
        runner, _ = self._make_runner("active")
        v = type("V", (), {"status": "no_effect"})()
        snap = PageSnapshot("t1", "u", "t", "fp", {}, None, (), True)
        d = Decision(action_type="click", target_id="e5")
        runner._handle_loop_guard(d, v, snap)  # 第 1 次
        rec = runner._handle_loop_guard(d, v, snap)  # 第 2 次
        self.assertIsNotNone(rec)
        self.assertEqual(rec.kind, "reobserve")

    def test_third_no_effect_blocks_target(self):
        """第 3 次相同 no_effect → 屏蔽 target。"""
        runner, _ = self._make_runner("active")
        v = type("V", (), {"status": "no_effect"})()
        snap = PageSnapshot("t1", "u", "t", "fp", {}, None, (), True)
        d = Decision(action_type="click", target_id="e5")
        runner._handle_loop_guard(d, v, snap)
        runner._handle_loop_guard(d, v, snap)
        rec = runner._handle_loop_guard(d, v, snap)
        self.assertIsNotNone(rec)
        self.assertIn("e5", runner.attempted)

    def test_page_change_resets_count(self):
        """页面变化 → 计数重置。"""
        runner, _ = self._make_runner("active")
        v = type("V", (), {"status": "no_effect"})()
        snap1 = PageSnapshot("t1", "u", "t", "fp1", {}, None, (), True)
        snap2 = PageSnapshot("t1", "u", "t", "fp2", {}, None, (), True)
        d = Decision(action_type="click", target_id="e5")
        runner._handle_loop_guard(d, v, snap1)  # 第 1 次
        # 指纹变化 → 签名变化 → 重置
        rec = runner._handle_loop_guard(d, v, snap2)  # 签名变化，视为第 1 次
        self.assertIsNone(rec)

    def test_action_change_resets_count(self):
        """动作变化 → 计数重置。"""
        runner, _ = self._make_runner("active")
        v = type("V", (), {"status": "no_effect"})()
        snap = PageSnapshot("t1", "u", "t", "fp", {}, None, (), True)
        runner._handle_loop_guard(Decision(action_type="click", target_id="e5"), v, snap)
        # 不同 target → 签名变化
        rec = runner._handle_loop_guard(Decision(action_type="click", target_id="e6"), v, snap)
        self.assertIsNone(rec)

    def test_shadow_mode_no_execution(self):
        """shadow 模式只记录不执行。"""
        runner, _ = self._make_runner("shadow")
        v = type("V", (), {"status": "no_effect"})()
        snap = PageSnapshot("t1", "u", "t", "fp", {}, None, (), True)
        d = Decision(action_type="click", target_id="e5")
        runner._handle_loop_guard(d, v, snap)
        runner._handle_loop_guard(d, v, snap)
        rec = runner._handle_loop_guard(d, v, snap)
        # shadow 模式返回 None（不干预），但 target 未屏蔽
        self.assertIsNone(rec)
        self.assertNotIn("e5", runner.attempted)


# ── 修复 3：click 焦点证据 ──────────────────────────────────────────────


class TestClickFocusEvidence(unittest.TestCase):
    def setUp(self):
        self.t1 = TargetInfo("t1", "page")

    def _snap(self, focused=None):
        return PageSnapshot("t1", "https://a.com", "A", "fp", {}, focused, (self.t1,), True)

    def test_focus_changed_partial(self):
        """仅焦点变化（无其他证据）→ partial，不判 completed。"""
        before = self._snap(focused=None)
        after = self._snap(focused={"tag": "input", "id": "kw", "type": "text"})
        effects = ActionEffects(focus_changed=True)
        a = assess_from_rules("click", before, after, effects, "点击搜索框")
        self.assertEqual(a.status, "partial")
        self.assertFalse(any(e.weight >= 0.7 for e in a.evidence))  # 无强证据

    def test_click_no_change_not_completed(self):
        """点击普通按钮无变化 → 不误判完成。"""
        before = self._snap(focused=None)
        after = self._snap(focused=None)
        effects = ActionEffects()
        a = assess_from_rules("click", before, after, effects, "点击按钮")
        self.assertEqual(a.status, "unknown")
        self.assertEqual(a.evidence, [])

    def test_click_url_change_still_completed(self):
        """点击 URL 变化仍判完成（原有逻辑保持）。"""
        before = self._snap(focused=None)
        after = self._snap(focused=None)
        effects = ActionEffects(url_changed=True)
        a = assess_from_rules("click", before, after, effects, "点击链接")
        self.assertEqual(a.status, "completed")

    def test_focus_changed_expected_effect(self):
        """focus_changed 应计入 click 的预期效果。"""
        from agent_runner import _expected_effect_seen
        effects = ActionEffects(focus_changed=True)
        self.assertTrue(_expected_effect_seen("click", effects))


if __name__ == "__main__":
    unittest.main(verbosity=2)