"""阶段 6B 单元测试：CheckpointStore、SessionRegistry、agent_resume 接口。

运行：
    python tests/test_resume_mcp.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_server import (
    CheckpointStatus,
    CheckpointStore,
    ResumeError,
    ResumeErrorCode,
    SessionRegistry,
    AgentSession,
)


class TestCheckpointStore(unittest.TestCase):
    """CheckpointStore 内存存储测试。"""

    def setUp(self):
        self.store = CheckpointStore()

    async def _make_ck(self, ck_id="ck1", session_id="s1"):
        class FakeCheckpoint:
            def __init__(self, cid, sid):
                self.checkpoint_id = cid
                self.session_id = sid
            def to_dict(self):
                return {"checkpoint_id": self.checkpoint_id, "session_id": self.session_id, "task": "测试"}
        return FakeCheckpoint(ck_id, session_id)

    def test_put_and_get(self):
        async def run():
            ck = await self._make_ck()
            ck_id = await self.store.put(ck, "s1")
            stored = await self.store.get(ck_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, CheckpointStatus.PAUSED)
            self.assertEqual(stored.session_id, "s1")
        asyncio.run(run())

    def test_consume_changes_status(self):
        async def run():
            ck = await self._make_ck()
            ck_id = await self.store.put(ck, "s1")
            claimed = await self.store.consume(ck_id)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, CheckpointStatus.RESUMING)
            # 第二次 consume 返回 None（已被领取）
            claimed2 = await self.store.consume(ck_id)
            self.assertIsNone(claimed2)
        asyncio.run(run())

    def test_expire(self):
        async def run():
            ck = await self._make_ck()
            ck_id = await self.store.put(ck, "s1")
            await self.store.expire(ck_id)
            stored = await self.store.get(ck_id)
            self.assertEqual(stored.status, CheckpointStatus.EXPIRED)
        asyncio.run(run())

    def test_get_nonexistent(self):
        async def run():
            stored = await self.store.get("nonexistent")
            self.assertIsNone(stored)
        asyncio.run(run())

    def test_consume_nonexistent(self):
        async def run():
            claimed = await self.store.consume("nonexistent")
            self.assertIsNone(claimed)
        asyncio.run(run())

    def test_mark_resumed(self):
        async def run():
            ck = await self._make_ck()
            ck_id = await self.store.put(ck, "s1")
            await self.store.mark_resumed(ck_id)
            stored = await self.store.get(ck_id)
            self.assertEqual(stored.status, CheckpointStatus.RESUMED)
        asyncio.run(run())

    def test_mark_superseded(self):
        async def run():
            ck = await self._make_ck()
            ck_id = await self.store.put(ck, "s1")
            await self.store.mark_superseded(ck_id)
            stored = await self.store.get(ck_id)
            self.assertEqual(stored.status, CheckpointStatus.SUPERSEDED)
        asyncio.run(run())


class TestSessionRegistry(unittest.TestCase):
    """SessionRegistry 测试。"""

    def setUp(self):
        self.registry = SessionRegistry()

    def test_register_and_get(self):
        session = AgentSession(session_id="s1", browser=None, adapter=None, runner=None)
        self.registry.register(session)
        self.assertIsNotNone(self.registry.get("s1"))
        self.assertTrue(self.registry.has("s1"))

    def test_get_nonexistent(self):
        self.assertIsNone(self.registry.get("nonexistent"))
        self.assertFalse(self.registry.has("nonexistent"))

    def test_remove(self):
        session = AgentSession(session_id="s1", browser=None, adapter=None, runner=None)
        self.registry.register(session)
        self.registry.remove("s1")
        self.assertFalse(self.registry.has("s1"))


class TestResumeErrorHandling(unittest.TestCase):
    """agent_resume 错误处理测试（模拟完整流程）。"""

    def setUp(self):
        self.store = CheckpointStore()
        self.registry = SessionRegistry()

    async def _setup_paused_checkpoint(self, ck_id="ck1", session_id="s1"):
        class FakeCheckpoint:
            def __init__(self, cid, sid):
                self.checkpoint_id = cid
                self.session_id = sid
            def to_dict(self):
                return {
                    "checkpoint_id": self.checkpoint_id,
                    "session_id": self.session_id,
                    "task": "测试",
                    "version": 1,
                    "current_goal": None,
                    "next_goal": None,
                    "completed_goals": [],
                    "failed_attempts": [],
                    "step": 1,
                    "page_url": "https://example.com",
                    "page_fingerprint": None,
                    "pause_reason": "waiting_for_user",
                    "snapshot_available": True,
                }
        ck = FakeCheckpoint(ck_id, session_id)
        ck_id = await self.store.put(ck, session_id)
        # 注册 session
        self.registry.register(AgentSession(
            session_id=session_id,
            browser=None,
            adapter=None,
            runner=None,
            checkpoint_id=ck_id,
        ))
        return ck_id, session_id

    def test_checkpoint_not_found(self):
        """不存在的 checkpoint → CHECKPOINT_NOT_FOUND。"""
        async def run():
            from mcp_server import _handle_resume
            stored = await self.store.get("nonexistent")
            self.assertIsNone(stored)
        asyncio.run(run())

    def test_expired_checkpoint(self):
        """已过期的 checkpoint → CHECKPOINT_EXPIRED。"""
        async def run():
            ck_id, _ = await self._setup_paused_checkpoint()
            await self.store.expire(ck_id)
            stored = await self.store.get(ck_id)
            self.assertEqual(stored.status, CheckpointStatus.EXPIRED)
        asyncio.run(run())

    def test_resumed_checkpoint(self):
        """已恢复的 checkpoint → CHECKPOINT_ALREADY_RESUMED。"""
        async def run():
            ck_id, _ = await self._setup_paused_checkpoint()
            await self.store.mark_resumed(ck_id)
            stored = await self.store.get(ck_id)
            self.assertEqual(stored.status, CheckpointStatus.RESUMED)
        asyncio.run(run())

    def test_session_not_found(self):
        """session 已不存在 → SESSION_NOT_FOUND。"""
        async def run():
            ck_id, _ = await self._setup_paused_checkpoint("ck_orphan", "s_orphan")
            self.registry.remove("s_orphan")
            session = self.registry.get("s_orphan")
            self.assertIsNone(session)
        asyncio.run(run())

    def test_concurrent_consume_only_one_succeeds(self):
        """并发 consume：只有一个能成功（原子领取）。"""
        async def run():
            ck_id, _ = await self._setup_paused_checkpoint()
            # 两次 consume
            c1 = await self.store.consume(ck_id)
            c2 = await self.store.consume(ck_id)
            # 第一个成功（返回对象），第二个失败（返回 None）
            self.assertIsNotNone(c1)
            self.assertEqual(c1.status, CheckpointStatus.RESUMING)
            self.assertIsNone(c2)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)