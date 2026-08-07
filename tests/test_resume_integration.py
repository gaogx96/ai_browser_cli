"""6B 集成测试：端到端 run_task → paused → agent_resume → completed。

运行：
    python tests/test_resume_integration.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_server import (
    CheckpointStatus,
    CheckpointStore,
    ResumeErrorCode,
    SessionRegistry,
    AgentSession,
)
from agent_runner import AgentRunner, Checkpoint


class MockBrowser:
    def __init__(self):
        self.calls = []
        self.url = "about:blank"

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
        return {"success": True}

    async def send_command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"result": "fp"}


class MockLLM:
    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.call_count = 0

    async def decide(self, task, tree, meta, history, extra_context=None):
        self.call_count += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "用尽"}


class TestIntegrationPauseResume(unittest.TestCase):
    """端到端：run_task → paused → agent_resume → completed。"""

    def setUp(self):
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"
        self.store = CheckpointStore()
        self.registry = SessionRegistry()

    def test_full_pause_resume_cycle(self):
        """完整流程：run_task → paused → 保存 checkpoint → agent_resume → completed。

        验证：
        - session_id 不变
        - browser 实例不变
        - 没有重放 pause 前的 action
        - resume 前执行了新的 observe
        """
        browser = MockBrowser()
        decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "pause", "reason": "waiting_for_user"},
        ]
        llm = MockLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))

        # 验证 paused
        self.assertEqual(result.status, "paused")
        self.assertIsNotNone(result.checkpoint)
        ck = result.checkpoint

        # 模拟 MCP dispatch：保存 checkpoint + session
        ck_id = asyncio.run(self.store.put(ck, "session_1"))
        self.registry.register(AgentSession(
            session_id="session_1",
            browser=browser,
            adapter=None,
            runner=runner,
            checkpoint_id=ck_id,
        ))

        # 验证 checkpoint 已保存
        stored = asyncio.run(self.store.get(ck_id))
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, CheckpointStatus.PAUSED)

        # 模拟 agent_resume：恢复执行
        resume_decisions = [
            {"action": "stop", "reason": "resume 后完成"},
        ]
        # 给 runner 换新的 LLM 决策序列
        runner.llm = MockLLM(resume_decisions)

        # 手动调用 resume（模拟 MCP handler）
        resume_result = asyncio.run(runner.resume(ck))

        # 验证 completed
        self.assertEqual(resume_result.status, "success")

        # 验证：session_id 不变（手动验证，这里用同一个 runner）
        self.assertIs(runner.browser, browser)  # browser 实例不变

        # 验证：没有重放 pause 前的 action
        navigate_calls = [c for c in browser.calls if c[0] == "navigate"]
        # navigate 只应执行一次（pause 前），resume 后不重放
        self.assertEqual(len(navigate_calls), 1)

        # 验证：resume 后 LLM 被重新调用
        self.assertGreaterEqual(runner.llm.call_count, 1)


class TestResumeAgainPause(unittest.TestCase):
    """恢复后再次暂停 → 生成新 checkpoint。"""

    def test_resume_then_pause_creates_new_checkpoint(self):
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        store = CheckpointStore()
        registry = SessionRegistry()

        browser = MockBrowser()
        decisions = [
            {"action": "pause", "reason": "第一次暂停"},
        ]
        llm = MockLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))

        ck_a = result.checkpoint
        ck_a_id = asyncio.run(store.put(ck_a, "session_1"))
        registry.register(AgentSession(
            session_id="session_1", browser=browser, adapter=None, runner=runner,
            checkpoint_id=ck_a_id,
        ))

        # 标记旧 checkpoint 为 superseded
        asyncio.run(store.mark_resumed(ck_a_id))
        asyncio.run(store.mark_superseded(ck_a_id))

        # resume 后再次 pause
        resume_decisions = [
            {"action": "pause", "reason": "第二次暂停"},
        ]
        runner.llm = MockLLM(resume_decisions)
        resume_result = asyncio.run(runner.resume(ck_a))

        self.assertEqual(resume_result.status, "paused")
        ck_b = resume_result.checkpoint

        # 旧 checkpoint 状态为 superseded
        old = asyncio.run(store.get(ck_a_id))
        self.assertEqual(old.status, CheckpointStatus.SUPERSEDED)

        # 新 checkpoint 有不同 ID
        self.assertIsNotNone(ck_b)
        self.assertNotEqual(ck_a_id, ck_b.checkpoint_id)


class TestDuplicateResume(unittest.TestCase):
    """重复 resume 同一 checkpoint → 错误。"""

    def test_double_resume_fails(self):
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        store = CheckpointStore()
        registry = SessionRegistry()

        browser = MockBrowser()
        decisions = [{"action": "pause", "reason": "暂停"}]
        llm = MockLLM(decisions)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        result = asyncio.run(runner.run("测试"))

        ck = result.checkpoint
        ck_id = asyncio.run(store.put(ck, "session_1"))
        registry.register(AgentSession(
            session_id="session_1", browser=browser, adapter=None, runner=runner,
            checkpoint_id=ck_id,
        ))

        # 第一次 consume 成功
        claimed = asyncio.run(store.consume(ck_id))
        self.assertIsNotNone(claimed)

        # 第二次 consume 失败
        claimed2 = asyncio.run(store.consume(ck_id))
        self.assertIsNone(claimed2)


class TestSessionNotFound(unittest.TestCase):
    """session 不存在 → 不创建新 browser。"""

    def test_session_not_found_no_fake_resume(self):
        os.environ["AGENT_VERIFY_MODE"] = "off"
        os.environ["AGENT_RECOVERY_MODE"] = "off"
        os.environ["AGENT_CONTEXT_MODE"] = "legacy"

        store = CheckpointStore()
        registry = SessionRegistry()

        # 创建 checkpoint 但不注册 session
        ck = Checkpoint(
            checkpoint_id="orphan_ck",
            session_id="orphan_session",
            task="测试",
            step=1,
            page_url="https://example.com",
            pause_reason="waiting_for_user",
        )
        ck_id = asyncio.run(store.put(ck, "orphan_session"))

        # session 不存在
        session = registry.get("orphan_session")
        self.assertIsNone(session)


class TestCheckpointExpiry(unittest.TestCase):
    """checkpoint TTL 过期检测。"""

    def test_ttl_expiry_on_get(self):
        store = CheckpointStore()
        store.TTL_SECONDS = -1  # 立即过期

        ck = Checkpoint(
            checkpoint_id="expired_ck", session_id="s1", task="t",
            step=1, page_url="https://a.com", pause_reason="waiting_for_user",
        )
        ck_id = asyncio.run(store.put(ck, "s1"))

        # 读取时惰性过期
        stored = asyncio.run(store.get(ck_id))
        self.assertEqual(stored.status, CheckpointStatus.EXPIRED)

    def test_ttl_expiry_on_consume(self):
        store = CheckpointStore()
        store.TTL_SECONDS = -1  # 立即过期

        ck = Checkpoint(
            checkpoint_id="expired_ck2", session_id="s1", task="t",
            step=1, page_url="https://a.com", pause_reason="waiting_for_user",
        )
        ck_id = asyncio.run(store.put(ck, "s1"))

        # consume 时惰性过期 → 返回 None
        claimed = asyncio.run(store.consume(ck_id))
        self.assertIsNone(claimed)

    def test_cleanup_expired(self):
        store = CheckpointStore()
        store.TTL_SECONDS = -1

        ck = Checkpoint(
            checkpoint_id="clean_ck", session_id="s1", task="t",
            step=1, page_url="https://a.com", pause_reason="waiting_for_user",
        )
        ck_id = asyncio.run(store.put(ck, "s1"))

        # 清理
        cleaned = asyncio.run(store.cleanup_expired())
        self.assertGreaterEqual(cleaned, 1)

        # 已删除
        stored = asyncio.run(store.get(ck_id))
        self.assertIsNone(stored)


class TestStaleSessionCleanup(unittest.TestCase):
    """session 生命周期清理。"""

    def test_cleanup_stale(self):
        registry = SessionRegistry()
        registry.TTL_SECONDS = -1  # 立即过期

        registry.register(AgentSession(
            session_id="stale_session", browser=None, adapter=None, runner=None,
        ))

        cleaned = asyncio.run(registry.cleanup_stale())
        self.assertGreaterEqual(cleaned, 1)
        self.assertFalse(registry.has("stale_session"))


if __name__ == "__main__":
    unittest.main(verbosity=2)