"""C3-3：百度动态页面 canary。

三轮：
1. 固定短任务（打开百度 → 输入 Rust → 触发搜索）
2. 完整任务（打开百度并搜索 Rust）
3. 重复运行（观察稳定性）

配置：
AGENT_ACTION_GUARD=active
AGENT_LOOP_GUARD=active
AGENT_RECOVERY_MODE=active
AGENT_RAW_EVALUATE=off
AGENT_GOAL_ASSESSMENT=shadow
AGENT_OBSERVABILITY=jsonl

运行：
    python tests/canary_baidu.py [round]
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "python"))


def configure(round_no: int):
    os.environ["AGENT_VERIFY_MODE"] = "shadow"
    os.environ["AGENT_RECOVERY_MODE"] = "active"
    os.environ["AGENT_GOAL_ASSESSMENT"] = "shadow"
    os.environ["AGENT_LOOP_GUARD"] = "active"
    os.environ["AGENT_ACTION_GUARD"] = "active"
    os.environ["AGENT_RAW_EVALUATE"] = "off"
    os.environ["AGENT_OBSERVABILITY"] = "jsonl"
    os.environ["AGENT_OBSERVABILITY_PATH"] = f"baidu_c3_3_round{round_no}.jsonl"
    os.environ["AGENT_CONTEXT_MODE"] = "dual"


async def run_task(task: str, round_no: int, max_steps: int = 15) -> dict:
    from browser_client import BrowserClient
    from agent_runner import AgentRunner
    from llm import LLMClient

    configure(round_no)
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
            "attempted": sorted(runner.attempted),
            "no_effect_counts": dict(runner._no_effect_counts),
            "actions": [h["action"].get("action", "?") if isinstance(h["action"], dict) else str(h["action"]) for h in result.history],
        }
    finally:
        await browser.close()


async def main():
    round_no = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    if round_no == 1:
        # 固定短任务：分步执行
        task = "打开百度首页，找到搜索框，输入Rust"
        print(f"=== C3-3 第一轮：固定短任务 ===")
        print(f"任务: {task}")
        r = await run_task(task, 1, max_steps=10)
    elif round_no == 2:
        task = "打开 https://www.baidu.com 并搜索 Rust"
        print(f"=== C3-3 第二轮：完整任务 ===")
        print(f"任务: {task}")
        r = await run_task(task, 2, max_steps=15)
    else:
        task = "打开 https://www.baidu.com 并搜索 Rust"
        print(f"=== C3-3 第三轮：重复运行 #{round_no} ===")
        r = await run_task(task, round_no, max_steps=15)

    print(f"\n结果:")
    print(f"  success={r['success']} status={r['status']} steps={r['steps']}")
    print(f"  reason={r['reason'][:100]}")
    print(f"  elapsed={r['elapsed_sec']}s")
    print(f"  actions={r['actions']}")
    print(f"  completed_goals={r['completed_goals']}")
    print(f"  attempted={r['attempted']}")
    print(f"  no_effect_counts={list(r['no_effect_counts'].keys())[:5]}")
    print(f"\nJSONL: {os.path.abspath(f'baidu_c3_3_round{round_no}.jsonl')}")


if __name__ == "__main__":
    asyncio.run(main())