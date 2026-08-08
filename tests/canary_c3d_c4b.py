"""C3-D + C4-B canary：Bing 和百度端到端验证。

用于验证：
- C3-D：set_value 成功、form_changed 跟踪、redundant guard 防重复输入
- C4-B：CDP 读取错误有限重试、大页面导航稳定

配置：
AGENT_ACTION_GUARD=active
AGENT_LOOP_GUARD=active
AGENT_RECOVERY_MODE=active
AGENT_RAW_EVALUATE=off
AGENT_GOAL_ASSESSMENT=shadow
AGENT_REDUNDANT_GUARD=active
AGENT_OBSERVABILITY=jsonl

运行：
    python tests/canary_c3d_c4b.py bing
    python tests/canary_c3d_c4b.py baidu
    python tests/canary_c3d_c4b.py baidu 3   # 百度重复 3 次
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "python"))


def configure(target: str, run_no: int):
    os.environ["AGENT_VERIFY_MODE"] = "shadow"
    os.environ["AGENT_RECOVERY_MODE"] = "active"
    os.environ["AGENT_GOAL_ASSESSMENT"] = "shadow"
    os.environ["AGENT_LOOP_GUARD"] = "active"
    os.environ["AGENT_ACTION_GUARD"] = "active"
    os.environ["AGENT_RAW_EVALUATE"] = "off"
    os.environ["AGENT_REDUNDANT_GUARD"] = "active"
    os.environ["AGENT_OBSERVABILITY"] = "jsonl"
    os.environ["AGENT_OBSERVABILITY_PATH"] = f"canary_{target}_run{run_no}.jsonl"
    os.environ["AGENT_CONTEXT_MODE"] = "dual"


async def run_task(task: str, target: str, run_no: int, max_steps: int = 15) -> dict:
    from browser_client import BrowserClient
    from agent_runner import AgentRunner
    from llm import LLMClient

    configure(target, run_no)
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    browser = BrowserClient()
    await browser.start()
    try:
        llm = LLMClient(provider=provider)
        runner = AgentRunner(browser=browser, llm=llm, max_steps=max_steps)
        start = time.time()
        result = await runner.run(task)
        elapsed = time.time() - start
        return {
            "success": result.success,
            "status": result.status,
            "steps": result.steps,
            "reason": result.reason,
            "elapsed_sec": round(elapsed, 2),
            "completed_goals": list(runner.state.completed_goals) if runner.state else [],
            "blocked_targets": dict(runner.state.blocked_targets) if runner.state else {},
            "last_successful_action": runner.state.last_successful_action if runner.state else None,
            "attempted": sorted(runner.attempted),
            "actions": [h["action"].get("action", "?") if isinstance(h["action"], dict) else str(h["action"]) for h in result.history],
        }
    finally:
        await browser.close()


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "bing"
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    if target == "bing":
        tasks = ["打开 https://www.bing.com 并搜索 Rust"]
    elif target == "baidu":
        tasks = ["打开 https://www.baidu.com 并搜索 Rust"]
    else:
        print(f"未知目标: {target} (bing 或 baidu)")
        sys.exit(1)

    print(f"=== canary: {target} ({runs} 次) ===")
    for i in range(runs):
        task = tasks[0]
        print(f"\n--- 运行 {i+1}/{runs}: {task} ---")
        try:
            r = await run_task(task, target, i + 1, max_steps=15)
            print(f"  success={r['success']} steps={r['steps']} status={r['status']}")
            print(f"  actions={r['actions']}")
            print(f"  completed_goals={r['completed_goals']}")
            print(f"  last_successful_action={r['last_successful_action']}")
            print(f"  blocked_targets={r['blocked_targets']}")
            print(f"  reason={r['reason'][:80]}")
        except Exception as e:
            print(f"  FAILED: {str(e)[:100]}")
        if i < runs - 1:
            delay = int(os.environ.get("AGENT_LLM_DELAY", "35"))
            print(f"  等待 {delay}s 避免限流...")
            await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())