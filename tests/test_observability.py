"""P8-D 单元测试：可观测性（结构化事件、安全过滤、统计）。

运行：
    python tests/test_observability.py
"""

import asyncio
import json
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
    GoalAssessment,
    GoalEvidence,
    PageSnapshot,
    RecoveryDecision,
    TargetInfo,
)


class ObsBrowser:
    def __init__(self):
        self.url = "about:blank"
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
        return {"result": "fp"}


class ObsLLM:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.call_count = 0

    async def decide(self, task, tree, meta, history, extra_context=None):
        self.call_count += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


def configure(obs_mode="stderr", goal_mode="shadow"):
    os.environ["AGENT_VERIFY_MODE"] = "shadow"
    os.environ["AGENT_RECOVERY_MODE"] = "off"
    os.environ["AGENT_CONTEXT_MODE"] = "legacy"
    os.environ["AGENT_GOAL_ASSESSMENT"] = goal_mode
    os.environ["AGENT_OBSERVABILITY"] = obs_mode
    os.environ["AGENT_OBSERVABILITY_PATH"] = ""


class TestEmitEvent(unittest.TestCase):
    """_emit_event 结构化输出。"""

    def test_stderr_text_output(self):
        """stderr 模式输出文本格式。"""
        configure("stderr")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="s1")
        f = io.StringIO()
        with redirect_stderr(f):
            runner._emit_event("test_event", {"status": "completed", "confidence": 0.9})
        out = f.getvalue()
        self.assertIn("[test_event]", out)
        self.assertIn("status=completed", out)
        self.assertIn("confidence=0.90", out)

    def test_jsonl_output(self):
        """jsonl 模式输出结构化 JSON。"""
        configure("jsonl")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="s1", step=3)
        f = io.StringIO()
        with redirect_stderr(f):
            runner._emit_event("goal_assessment", {
                "status": "completed",
                "confidence": 0.92,
                "source": "rule",
            })
        out = f.getvalue().strip()
        # 解析 [event] 前缀后的 JSON
        json_str = out.split("[event] ", 1)[1]
        event = json.loads(json_str)
        self.assertEqual(event["event"], "goal_assessment")
        self.assertEqual(event["session_id"], "s1")
        self.assertEqual(event["step"], 3)
        self.assertEqual(event["status"], "completed")
        self.assertEqual(event["confidence"], 0.92)

    def test_off_no_output(self):
        """off 模式不输出。"""
        configure("off")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试")
        f = io.StringIO()
        with redirect_stderr(f):
            runner._emit_event("test", {"status": "completed"})
        self.assertEqual(f.getvalue(), "")

    def test_event_has_session_step(self):
        """事件包含 session_id 和 step 关联信息。"""
        configure("jsonl")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="sess_abc", step=7)
        f = io.StringIO()
        with redirect_stderr(f):
            runner._emit_event("action_verification", {"status": "success"})
        out = f.getvalue().strip()
        event = json.loads(out.split("[event] ", 1)[1])
        self.assertEqual(event["session_id"], "sess_abc")
        self.assertEqual(event["step"], 7)


class TestNoSensitiveData(unittest.TestCase):
    """事件不包含敏感字段。"""

    def test_goal_event_no_sensitive(self):
        """goal_assessment 事件不包含密码/表单值/Cookie。"""
        configure("jsonl")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="s1")
        assessment = GoalAssessment(
            goal="测试", status="completed", confidence=0.9,
            evidence=[GoalEvidence("url_match", "URL 变化")],
            source="rule", stable=True,
        )
        f = io.StringIO()
        with redirect_stderr(f):
            runner._log_assessment(assessment)
        out = f.getvalue()
        lower = out.lower()
        self.assertNotIn("password", lower)
        self.assertNotIn("cookie", lower)
        self.assertNotIn("authorization", lower)
        self.assertNotIn("token", lower)
        self.assertNotIn("secret", lower)

    def test_verify_event_no_sensitive(self):
        """action_verification 事件不包含页面内容。"""
        configure("jsonl")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="s1")
        v = ActionVerification(
            transport_ok=True, page_responded=True, expected_effect_seen=True,
            status="success",
        )
        effects = ActionEffects(url_changed=True, dom_changed=True)
        f = io.StringIO()
        with redirect_stderr(f):
            runner._log_verification(v, effects)
        out = f.getvalue()
        # 事件只包含布尔/状态，不包含页面文本
        self.assertNotIn("text_content", out)
        self.assertNotIn("password", out.lower())

    def test_recovery_event_error_preview_truncated(self):
        """recovery 事件的错误预览被截断到 60 字符。"""
        configure("jsonl")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="s1")
        rec = RecoveryDecision(kind="reobserve", reason="stale target")
        from agent_runner import Decision
        d = Decision(action_type="click", target_id="e5")
        result = {"success": False, "error": "x" * 1000}  # 超长错误
        f = io.StringIO()
        with redirect_stderr(f):
            runner._log_recovery(rec, d, result)
        out = f.getvalue()
        # 从第一行（[event] JSON 行）提取 error_preview
        first_line = out.split("\n")[0].strip()
        if "[event]" in first_line:
            json_str = first_line.split("[event] ", 1)[1].strip()
            event = json.loads(json_str)
            self.assertLessEqual(len(event.get("error_preview", "")), 60)
        # 文本日志中的 error 也被截断
        self.assertNotIn("x" * 100, out)


class TestStats(unittest.TestCase):
    """事件统计。"""

    def test_event_count_increments(self):
        """每次 emit 递增 seq。"""
        configure("jsonl")
        runner = AgentRunner(browser=ObsBrowser(), llm=ObsLLM([]))
        runner.state = AgentState(task="测试", session_id="s1")
        f = io.StringIO()
        with redirect_stderr(f):
            runner._emit_event("a", {"x": 1})
            runner._emit_event("b", {"x": 2})
            runner._emit_event("c", {"x": 3})
        lines = [l for l in f.getvalue().strip().split("\n") if l]
        self.assertEqual(len(lines), 3)
        seqs = [json.loads(l.split("[event] ", 1)[1])["seq"] for l in lines]
        self.assertEqual(seqs, [1, 2, 3])


class TestGoalEventInShadow(unittest.TestCase):
    """shadow 模式下 goal_assessment 事件产生但不改变行为。"""

    def test_shadow_emits_goal_event(self):
        """shadow 模式在 run() 中产生 [goal] 事件。"""
        configure("stderr", goal_mode="shadow")
        browser = ObsBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = ObsLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        f = io.StringIO()
        with redirect_stderr(f):
            result = asyncio.run(runner.run("测试"))
        stderr = f.getvalue()
        # 应产生 goal 事件
        self.assertIn("[goal]", stderr)
        # 不改变行为：completed_goals 保持空（shadow 不应用）
        self.assertEqual(runner.state.completed_goals, [])

    def test_shadow_does_not_change_action_sequence(self):
        """shadow 模式不改变动作序列。"""
        configure("stderr", goal_mode="shadow")
        browser = ObsBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = ObsLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        asyncio.run(runner.run("测试"))
        # 动作序列：navigate → stop
        navs = [c for c in browser.calls if c[0] == "navigate"]
        self.assertEqual(len(navs), 1)

    def test_shadow_no_extra_llm_calls(self):
        """shadow 模式不增加 LLM 调用。"""
        configure("stderr", goal_mode="shadow")
        browser = ObsBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]
        llm = ObsLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        asyncio.run(runner.run("测试"))
        # LLM 只被调用 2 次（navigate 决策 + stop 决策）
        self.assertEqual(llm.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)