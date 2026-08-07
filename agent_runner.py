"""
Agent Runner — 浏览器自主规划 Agent。

核心逻辑：
1. 观察：调 page_tree / meta 获取当前页面状态
2. 决策：把任务 + 页面树 + 历史发给 LLM，LLM 返回下一步动作
3. 执行：根据 LLM 返回的动作调 BrowserClient
4. 记录：动作结果入历史，供下一步参考
5. 循环直到 LLM 输出 stop 或超步数
"""

import asyncio
import json
import os
import sys
from typing import Any

from llm import LLMClient

# 日志输出到 stderr，避免污染 MCP 的 stdout JSON-RPC 协议
_log = lambda *a, **kw: print(*a, **kw, file=sys.stderr, flush=True)


# ── Agent 结果 ─────────────────────────────────────────────────────────────


class AgentResult:
    """Agent 执行结果。"""

    def __init__(self, success: bool, reason: str = "", steps: int = 0, history: list | None = None):
        self.success = success
        self.reason = reason
        self.steps = steps
        self.history = history or []

    def __repr__(self) -> str:
        status = "✅ 成功" if self.success else "❌ 失败"
        return f"AgentResult({status}, 步数={self.steps}, 原因={self.reason})"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "reason": self.reason,
            "steps": self.steps,
            "history": [
                {
                    "step": h.get("step"),
                    "action": h.get("action", {}).get("action", "?"),
                    "success": h.get("success", False),
                    "error": h.get("error", ""),
                }
                for h in self.history
            ],
        }


# ── Agent Runner ───────────────────────────────────────────────────────────


class AgentRunner:
    """浏览器自主规划 Agent。"""

    def __init__(
        self,
        browser,
        llm: LLMClient | None = None,
        max_steps: int = 15,
        download_path: str = "",
    ):
        self.browser = browser
        self.llm = llm or LLMClient(provider="anthropic")
        self.max_steps = max_steps
        self.download_path = download_path or os.path.join(os.path.expanduser("~"), "Desktop")
        self.history: list[dict] = []
        self.attempted: set[str] = set()  # 已尝试的 target_id，防死循环
        self._retry_counts: dict[str, int] = {}  # 元素重试计数

    async def run(self, task: str) -> AgentResult:
        """执行一个自然语言浏览器任务。"""
        self.history = []
        self.attempted = set()
        self._retry_counts = {}

        for step in range(1, self.max_steps + 1):
            _log(f"\n--- Step {step}/{self.max_steps} ---")

            # 1. 观察
            tree, meta = await self._observe()

            # 2. 决策
            action = await self.llm.decide(task, tree, meta, self.history)
            _log(f"  决策: {json.dumps(action, ensure_ascii=False)[:200]}")

            # 3. 终止判断
            if action.get("action") == "stop":
                reason = action.get("reason", "Agent 主动停止")
                _log(f"  Agent 停止: {reason}")
                return AgentResult(
                    success=True,
                    reason=reason,
                    steps=step,
                    history=self.history,
                )

            # 4. 执行
            result = await self._execute(action)

            # 5. 记录
            history_entry = {
                "step": step,
                "action": action,
                "success": result.get("success", False),
                "error": result.get("error", ""),
            }
            self.history.append(history_entry)

            if result.get("success"):
                _log(f"  执行成功")
            else:
                _log(f"  执行失败: {result.get('error', '')}")

            # 6. 防死循环：同一元素失败 3 次则强制跳过
            target_id = action.get("target_id", "")
            if not result.get("success") and target_id:
                self._retry_counts[target_id] = self._retry_counts.get(target_id, 0) + 1
                if self._retry_counts[target_id] >= 3:
                    self.attempted.add(target_id)
                    _log(f"  元素 {target_id} 已失败 3 次，加入黑名单")

            # 7. 等待页面稳定
            await asyncio.sleep(0.5)

        return AgentResult(
            success=False,
            reason=f"达到最大步数 {self.max_steps}",
            steps=self.max_steps,
            history=self.history,
        )

    async def _observe(self) -> tuple[str, dict]:
        """获取当前页面状态。"""
        try:
            meta = await self.browser.meta()
        except Exception as e:
            meta = {"url": "", "title": "", "interactiveCount": 0}

        try:
            tree = await self.browser.tree()
        except Exception as e:
            tree = f"[获取页面树失败: {e}]"

        return tree, meta

    async def _execute(self, action: dict) -> dict:
        """执行 LLM 返回的动作。"""
        act = action.get("action", "")

        try:
            if act == "navigate":
                url = action.get("url", "")
                if not url:
                    return {"success": False, "error": "缺少 url 参数"}
                resp = await self.browser.navigate(url)
                return {"success": True, "data": resp}

            elif act == "click":
                target_id = action.get("target_id", "")
                if not target_id:
                    return {"success": False, "error": "缺少 target_id 参数"}
                if target_id in self.attempted:
                    return {"success": False, "error": f"元素 {target_id} 已黑名单"}
                resp = await self.browser.click(target_id)
                return {"success": True, "data": resp}

            elif act == "type":
                target_id = action.get("target_id", "")
                text = action.get("text", "")
                if not target_id or not text:
                    return {"success": False, "error": "缺少 target_id 或 text 参数"}
                resp = await self.browser.type_text(target_id, text)
                return {"success": True, "data": resp}

            elif act == "evaluate":
                expression = action.get("expression", "")
                if not expression:
                    return {"success": False, "error": "缺少 expression 参数"}
                resp = await self.browser.send_command("evaluate", expression=expression)
                return {"success": True, "data": resp}

            elif act == "download_setup":
                path = action.get("path", self.download_path)
                os.makedirs(path, exist_ok=True)
                resp = await self.browser.send_command("download_setup", path=path)
                return {"success": True, "data": resp}

            else:
                return {"success": False, "error": f"未知动作: {act}"}

        except Exception as e:
            return {"success": False, "error": str(e)}


# ── 独立运行入口 ────────────────────────────────────────────────────────────


async def main():
    """独立运行 Agent 的入口（用于测试）。"""
    from browser_client import BrowserClient

    # 解析参数
    task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else input("请输入任务描述: ")

    if not task:
        print("请提供任务描述")
        return

    provider = os.environ.get("LLM_PROVIDER")
    if not provider:
        print("请设置 LLM_PROVIDER 环境变量（anthropic 或 openai）")
        return
    max_steps = int(os.environ.get("AGENT_MAX_STEPS", "15"))

    print(f"LLM: {provider}, 最大步数: {max_steps}")
    print(f"任务: {task}")

    # 启动浏览器
    browser = BrowserClient()
    await browser.start()

    try:
        llm = LLMClient(provider=provider)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=max_steps)
        result = await runner.run(task)
        print("\n" + "=" * 50)
        print(f"结果: {'✅ 成功' if result.success else '❌ 失败'}")
        print(f"步数: {result.steps}")
        print(f"原因: {result.reason}")
        print("=" * 50)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())