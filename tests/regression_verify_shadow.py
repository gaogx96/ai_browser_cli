"""Shadow 层无副作用回归测试（mock，不消耗 LLM API）。

用固定决策序列 + mock browser 驱动 AgentRunner，
对比 off / shadow 两种模式下：成功失败、动作序列、history、重试行为是否完全一致。

运行：
    python tests/regression_verify_shadow.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import AgentRunner


class MockBrowser:
    """mock 浏览器：记录调用，返回可预测结果。"""

    def __init__(self):
        self.calls = []
        self.url = "about:blank"
        self.title = ""

    async def meta(self):
        return {"url": self.url, "title": self.title, "interactiveCount": 1}

    async def tree(self):
        return "[@e1] link \"example\"\n[@e2] button \"go\""

    async def navigate(self, url, **kw):
        self.calls.append(("navigate", url))
        self.url = url
        self.title = "Example Domain"
        return {"status": "ok", "url": url}

    async def click(self, target_id, **kw):
        self.calls.append(("click", target_id))
        return {"status": "ok", "scrolled": True}

    async def type_text(self, target_id, text, **kw):
        self.calls.append(("type", target_id, text))
        return {"status": "ok"}

    async def send_command(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "evaluate":
            return {"result": "fp"}
        if action == "targets":
            return {"targets": [{"id": "t1", "type": "page", "url": self.url}]}
        return {"status": "ok"}


class FakeLLM:
    """mock LLM：按固定序列返回决策，验证 shadow 不改变决策序列消费。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = 0

    async def decide(self, task, tree, meta, history):
        self.calls += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"action": "stop", "reason": "决策用尽"}


async def run_agent(verify_mode: str, decisions) -> dict:
    """用固定决策驱动 AgentRunner，返回结果摘要。"""
    os.environ["AGENT_VERIFY_MODE"] = verify_mode
    browser = MockBrowser()
    llm = FakeLLM(decisions)
    runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
    result = await runner.run("测试任务")
    return {
        "success": result.success,
        "reason": result.reason,
        "steps": result.steps,
        "history_count": len(result.history),
        "browser_calls": browser.calls,
        "llm_calls": llm.calls,
        "history": result.to_dict()["history"],
    }


class TestShadowNoSideEffects(unittest.TestCase):
    """核心：shadow 层不改变 Agent 行为。"""

    def setUp(self):
        self.decisions = [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "stop", "reason": "完成"},
        ]

    async def _compare(self):
        off = await run_agent("off", list(self.decisions))
        shadow = await run_agent("shadow", list(self.decisions))
        return off, shadow

    def test_result_identical(self):
        off, shadow = asyncio.run(self._compare())
        for key in ["success", "steps", "history_count", "llm_calls"]:
            self.assertEqual(off[key], shadow[key], f"{key} 不一致")

    def test_history_identical(self):
        off, shadow = asyncio.run(self._compare())
        self.assertEqual(off["history"], shadow["history"])

    def test_browser_navigate_calls_identical(self):
        """核心动作（navigate/click/type）序列应一致，shadow 只额外加 evaluate。"""
        off, shadow = asyncio.run(self._compare())
        off_core = [c for c in off["browser_calls"] if c[0] != "evaluate" and c[0] != "targets"]
        shadow_core = [c for c in shadow["browser_calls"] if c[0] != "evaluate" and c[0] != "targets"]
        self.assertEqual(off_core, shadow_core)

    def test_verify_off_disables_snapshot(self):
        """off 模式不应调用 evaluate/targets（无 snapshot 采集）。"""
        browser = MockBrowser()
        llm = FakeLLM(list(self.decisions))
        os.environ["AGENT_VERIFY_MODE"] = "off"
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        asyncio.run(runner.run("测试任务"))
        # off 模式只应有 navigate/stop 相关调用，无 evaluate/targets
        eval_calls = [c for c in browser.calls if c[0] == "evaluate"]
        self.assertEqual(eval_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)