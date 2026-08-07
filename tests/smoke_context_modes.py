"""冒烟测试：对比 AGENT_CONTEXT_MODE=legacy/dual/structured 三种模式。

运行方式：
    python tests/smoke_context_modes.py
    # 需要 Chrome 已打开（扩展模式或 9222 端口）

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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "python"))


async def run_mode(mode: str, task: str, max_steps: int = 10) -> dict:
    """以指定 context_mode 运行一次 Agent 任务，返回结果和关键指标。"""
    os.environ["AGENT_CONTEXT_MODE"] = mode
    os.environ["AGENT_VERIFY_MODE"] = "shadow"

    from browser_client import BrowserClient
    from agent_runner import AgentRunner
    from llm import LLMClient

    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    browser = BrowserClient()
    await browser.start()
    try:
        llm = LLMClient(provider=provider)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=max_steps)
        start = time.time()
        result = await runner.run(task)
        elapsed = time.time() - start

        # 估算 token 使用（通过 history 动作序列长度估算）
        history_json = json.dumps(result.history)
        est_tokens = len(history_json) // 4

        return {
            "mode": mode,
            "success": result.success,
            "reason": result.reason,
            "steps": result.steps,
            "elapsed_sec": round(elapsed, 2),
            "history_count": len(result.history),
            "est_tokens": est_tokens,
            "state": {
                "current_goal": runner.state.current_goal if runner.state else None,
                "next_goal": runner.state.next_goal if runner.state else None,
                "goal_status": runner.state.goal_status if runner.state else None,
                "completed_goals": runner.state.completed_goals if runner.state else [],
                "failed_attempts": runner.state.failed_attempts if runner.state else [],
            },
            "actions": [h["action"].get("action", "?") if isinstance(h["action"], dict) else str(h["action"]) for h in result.history],
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
        print(f"Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    task = "打开 https://example.com 并告诉我页面标题"

    mode = sys.argv[1] if len(sys.argv) > 1 else "dual"
    if mode not in ("legacy", "dual", "structured"):
        print(f"Usage: python smoke_context_modes.py [legacy|dual|structured]")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Running AGENT_CONTEXT_MODE={mode}...")
    print(f"{'='*60}")
    try:
        r = await run_mode(mode, task)
        print(f"  success={r['success']}")
        print(f"  steps={r['steps']}")
        print(f"  elapsed={r['elapsed_sec']}s")
        print(f"  actions={r['actions']}")
        print(f"  reason={r['reason'][:80]}")
        if r.get("state"):
            print(f"  state.goal_status={r['state']['goal_status']}")
            print(f"  state.current_goal={r['state']['current_goal']}")
            print(f"  state.next_goal={r['state']['next_goal']}")
            print(f"  state.completed_goals={r['state']['completed_goals']}")
            print(f"  state.failed_attempts={r['state']['failed_attempts']}")
    except Exception as e:
        print(f"  FAILED: {str(e)[:200]}")


if __name__ == "__main__":
    asyncio.run(main())