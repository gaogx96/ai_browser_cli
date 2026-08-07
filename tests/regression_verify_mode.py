"""回归测试：对比 AGENT_VERIFY_MODE=off 与 shadow 的任务结果一致性。

运行方式：
    python tests/regression_verify_mode.py
    # 需要已启动的 Chrome（扩展模式或 9222 端口）

环境变量：
    LLM_PROVIDER=anthropic
    ANTHROPIC_MODEL=deepseek-v4-flash
    AGENT_BROWSER_EXTENSION=1
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# browser_client 在 sdk/python/ 下
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "python"))

from agent_runner import AgentRunner
from llm import LLMClient

# 测试任务：简单可复现
TEST_TASKS = [
    "打开 https://example.com 并告诉我页面标题",
]

async def run_with_mode(mode: str, task: str) -> dict:
    """以指定 verify_mode 运行一次 Agent 任务，返回结果。"""
    os.environ["AGENT_VERIFY_MODE"] = mode
    from browser_client import BrowserClient

    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    browser = BrowserClient()
    await browser.start()
    try:
        llm = LLMClient(provider=provider)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=10)
        start = time.time()
        result = await runner.run(task)
        elapsed = time.time() - start
        return {
            "mode": mode,
            "success": result.success,
            "reason": result.reason,
            "steps": result.steps,
            "history_count": len(result.history),
            "elapsed_sec": round(elapsed, 2),
            "history": result.to_dict()["history"],
        }
    finally:
        await browser.close()


async def main():
    missing = []
    if not os.environ.get("LLM_PROVIDER"):
        missing.append("LLM_PROVIDER")
    if not os.environ.get("ANTHROPIC_MODEL") and not os.environ.get("OPENAI_MODEL"):
        missing.append("ANTHROPIC_MODEL/OPENAI_MODEL")
    if missing:
        print(f"❌ 缺少环境变量: {', '.join(missing)}")
        print("设置后重试，例如:")
        print("  $env:LLM_PROVIDER='anthropic'")
        print("  $env:ANTHROPIC_MODEL='deepseek-v4-flash'")
        print("  $env:AGENT_BROWSER_EXTENSION=1")
        sys.exit(1)

    all_pass = True
    for task in TEST_TASKS:
        print(f"\n{'='*60}")
        print(f"任务: {task}")
        print(f"{'='*60}")

        print(f"\n▶ 运行 off 模式...")
        off_result = await run_with_mode("off", task)

        print(f"\n▶ 运行 shadow 模式...")
        shadow_result = await run_with_mode("shadow", task)

        # 对比
        print(f"\n{'─'*40}")
        print(f"对比结果:")
        print(f"  off:     success={off_result['success']}, steps={off_result['steps']}, "
              f"reason={off_result['reason'][:60]}")
        print(f"  shadow:  success={shadow_result['success']}, steps={shadow_result['steps']}, "
              f"reason={shadow_result['reason'][:60]}")

        mismatches = []
        for key in ["success", "steps", "history_count"]:
            if off_result[key] != shadow_result[key]:
                mismatches.append(f"{key}: off={off_result[key]} vs shadow={shadow_result[key]}")

        # 对比 history 动作序列
        off_actions = [h["action"] for h in off_result["history"]]
        shadow_actions = [h["action"] for h in shadow_result["history"]]
        if off_actions != shadow_actions:
            mismatches.append(f"action_sequence differs: off={off_actions} vs shadow={shadow_actions}")

        if mismatches:
            print(f"\n❌ 不一致:")
            for m in mismatches:
                print(f"   - {m}")
            all_pass = False
        else:
            print(f"\n✅ 一致")

    print(f"\n{'='*60}")
    if all_pass:
        print(f"✅ 所有回归测试通过")
    else:
        print(f"❌ 存在不一致")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())