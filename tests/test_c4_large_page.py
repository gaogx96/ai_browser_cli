"""C4 单元测试：大页面截断、CDP chunk 错误检测、结构化 PAGE_TOO_LARGE。

运行：
    python tests/test_c4_large_page.py
"""

import asyncio
import os
import sys
import unittest
from contextlib import redirect_stderr
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import AgentRunner


class MockBrowser:
    """可配置的 mock 浏览器，模拟大页面/小页面场景。"""

    def __init__(self, tree_text="[@e1] link \"a\"\n[@e2] button \"b\"", meta=None):
        self.tree_text = tree_text
        self.meta_data = meta or {"url": "https://example.com", "title": "T", "interactiveCount": 2}
        self.calls = []

    async def meta(self):
        return dict(self.meta_data)

    async def tree(self):
        return self.tree_text

    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        return {"success": True, "title": "T"}

    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
        return {"success": True}

    async def type_text(self, target_id, text, **kw):
        return {"success": True}

    async def send_command(self, action, **kwargs):
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


def _make_runner(tree_text):
    os.environ["AGENT_VERIFY_MODE"] = "off"
    os.environ["AGENT_RECOVERY_MODE"] = "off"
    os.environ["AGENT_CONTEXT_MODE"] = "legacy"
    os.environ["AGENT_LOOP_GUARD"] = "off"
    os.environ["AGENT_ACTION_GUARD"] = "off"
    os.environ["AGENT_OBSERVABILITY"] = "off"
    browser = MockBrowser(tree_text=tree_text)
    runner = AgentRunner(browser=browser, llm=MockLLM([]))
    return runner, browser


class TestObservePAGE_TOO_LARGE(unittest.TestCase):
    """_observe 检测 CDP chunk 错误。"""

    def test_normal_tree_no_error(self):
        """正常页面树不触发 PAGE_TOO_LARGE。"""
        runner, _ = _make_runner("[@e1] link \"a\"")
        tree, meta = asyncio.run(runner._observe())
        self.assertNotIn("PAGE_TOO_LARGE", tree)
        self.assertIn("[@e1]", tree)

    def test_chunk_error_detected(self):
        """CDP chunk 错误 → PAGE_TOO_LARGE。"""
        runner, _ = _make_runner("")
        # 模拟 tree() 抛 CDP chunk 错误
        class ChunkBrowser(MockBrowser):
            async def tree(self):
                raise Exception("Separator is not found, and chunk exceed the limit")
        runner.browser = ChunkBrowser()
        tree, meta = asyncio.run(runner._observe())
        self.assertIn("PAGE_TOO_LARGE", tree)
        self.assertIn("evaluate", tree)  # 建议使用 evaluate

    def test_chunk_exceed_variant(self):
        """chunk exceed 变体也检测。"""
        runner, _ = _make_runner("")
        class ChunkBrowser2(MockBrowser):
            async def tree(self):
                raise Exception("Separator is found, but chunk is longer than limit")
        runner.browser = ChunkBrowser2()
        tree, meta = asyncio.run(runner._observe())
        self.assertIn("PAGE_TOO_LARGE", tree)

    def test_generic_error_not_paged_too_large(self):
        """普通错误不误判为 PAGE_TOO_LARGE。"""
        runner, _ = _make_runner("")
        class ErrBrowser(MockBrowser):
            async def tree(self):
                raise Exception("some other error")
        runner.browser = ErrBrowser()
        tree, meta = asyncio.run(runner._observe())
        self.assertNotIn("PAGE_TOO_LARGE", tree)
        self.assertIn("获取页面树失败", tree)

    def test_no_original_html_leak(self):
        """PAGE_TOO_LARGE 不泄露原始 HTML。"""
        runner, _ = _make_runner("")
        class ChunkBrowser3(MockBrowser):
            async def tree(self):
                raise Exception("Separator is not found, and chunk exceed the limit")
        runner.browser = ChunkBrowser3()
        tree, meta = asyncio.run(runner._observe())
        # 错误信息只包含错误摘要，不包含完整 HTML
        self.assertLess(len(tree), 200)


class TestTruncatedTreeHandling(unittest.TestCase):
    """截断页面树的处理。"""

    def test_truncated_tree_has_marker(self):
        """截断树包含标记。"""
        # 模拟 Rust 侧 EXTRACT_TREE_SCRIPT 返回的截断树
        truncated = "[@e1] link \"a\"\n... (500 elements, showing first 300)"
        runner, _ = _make_runner(truncated)
        tree, meta = asyncio.run(runner._observe())
        self.assertIn("... (", tree)
        # 不误判为 PAGE_TOO_LARGE（这是正常截断，不是错误）
        self.assertNotIn("PAGE_TOO_LARGE", tree)

    def test_truncated_tree_still_parses(self):
        """截断树仍可解析出元素。"""
        truncated = "[@e1] link \"a\"\n[@e2] button \"b\"\n... (500 elements, showing first 300)"
        runner, _ = _make_runner(truncated)
        tree, meta = asyncio.run(runner._observe())
        self.assertIn("[@e1]", tree)
        self.assertIn("[@e2]", tree)

    def test_target_not_found_not_equivalent_to_page_fail(self):
        """目标不存在 ≠ 页面失败。"""
        # 截断树看不到某个目标，但页面本身正常
        truncated = "[@e1] link \"a\"\n... (500 elements, showing first 300)"
        runner, _ = _make_runner(truncated)
        tree, meta = asyncio.run(runner._observe())
        # 页面正常（有 meta，有部分元素），不是失败
        self.assertNotIn("获取页面树失败", tree)
        self.assertEqual(meta["interactiveCount"], 2)


class TestSmallPageNoTruncation(unittest.TestCase):
    """小页面不截断。"""

    def test_small_page_full_tree(self):
        """小页面返回完整树。"""
        small_tree = "[@e1] link \"a\"\n[@e2] button \"b\"\n[@e3] input \"搜索\""
        runner, _ = _make_runner(small_tree)
        tree, meta = asyncio.run(runner._observe())
        self.assertIn("[@e3]", tree)
        self.assertNotIn("... (", tree)  # 无截断标记


if __name__ == "__main__":
    unittest.main(verbosity=2)