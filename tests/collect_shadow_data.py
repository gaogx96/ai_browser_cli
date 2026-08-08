"""Shadow 数据收集脚本。

在测试环境启用 GoalAssessment shadow + 可观测性，用固定任务收集 JSONL 样本。

运行方式：
    python tests/collect_shadow_data.py
    # 需要真实浏览器 + LLM

环境变量：
    LLM_PROVIDER=anthropic
    ANTHROPIC_MODEL=deepseek-v4-flash
    AGENT_BROWSER_EXTENSION=1

输出：
    AGENT_OBSERVABILITY_PATH 指定的 JSONL 文件（默认 shadow_data.jsonl）
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "python"))


# 收集的测试任务
TEST_TASKS = [
    "打开 https://example.com 并告诉我页面标题",
    "打开 https://www.baidu.com 并读取页面标题",
    "打开 https://www.bing.com 并搜索 Rust",
    "打开 https://www.baidu.com 并搜索 Rust",
]

JSONL_PATH = os.environ.get("AGENT_OBSERVABILITY_PATH", "shadow_data.jsonl")


async def run_task(task: str, task_id: int) -> dict:
    """运行一个任务，收集 shadow 数据到 JSONL。"""
    os.environ["AGENT_VERIFY_MODE"] = "shadow"
    os.environ["AGENT_RECOVERY_MODE"] = "shadow"
    os.environ["AGENT_GOAL_ASSESSMENT"] = "shadow"
    os.environ["AGENT_OBSERVABILITY"] = "jsonl"
    os.environ["AGENT_OBSERVABILITY_PATH"] = JSONL_PATH
    os.environ["AGENT_CONTEXT_MODE"] = "dual"

    from browser_client import BrowserClient
    from agent_runner import AgentRunner
    from llm import LLMClient

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
            "task_id": task_id,
            "task": task,
            "success": result.success,
            "reason": result.reason,
            "steps": result.steps,
            "elapsed_sec": round(elapsed, 2),
            "status": result.status,
            "history_count": len(result.history),
            "completed_goals": list(runner.state.completed_goals) if runner.state else [],
        }
    finally:
        await browser.close()


async def main():
    if not os.environ.get("LLM_PROVIDER"):
        print("请设置 LLM_PROVIDER 环境变量")
        sys.exit(1)

    print(f"Shadow 数据收集开始")
    print(f"JSONL 输出: {os.path.abspath(JSONL_PATH)}")
    print(f"任务数: {len(TEST_TASKS)}")
    print()

    results = []
    for i, task in enumerate(TEST_TASKS):
        print(f"[{i+1}/{len(TEST_TASKS)}] 运行: {task[:60]}...")
        try:
            r = await run_task(task, i)
            results.append(r)
            print(f"  -> success={r['success']} steps={r['steps']} status={r['status']}")
            print(f"  -> completed_goals={r['completed_goals']}")
        except Exception as e:
            print(f"  -> FAILED: {str(e)[:100]}")
        # 避免 API 限流
        if i < len(TEST_TASKS) - 1:
            delay = int(os.environ.get("AGENT_LLM_DELAY", "30"))
            print(f"  等待 {delay}s 避免限流...")
            await asyncio.sleep(delay)

    # 统计
    print(f"\n{'='*60}")
    print(f"数据收集完成")
    print(f"JSONL 文件: {os.path.abspath(JSONL_PATH)}")
    print(f"成功: {sum(1 for r in results if r['success'])}/{len(results)}")
    print(f"暂停: {sum(1 for r in results if r.get('status') == 'paused')}")
    print(f"失败: {sum(1 for r in results if not r['success'])}")
    if results:
        print(f"平均步数: {sum(r['steps'] for r in results) / len(results):.1f}")
    print(f"\nJSONL 文件: {os.path.abspath(JSONL_PATH)}")
    print("分析命令: python -c \"import json; [print(json.dumps({k: e[k] for k in ('event','status','source','confidence','applied','rejected_reason') if k in e}, ensure_ascii=False)) for e in [json.loads(l) for l in open('shadow_data.jsonl') if l.strip()]]\"")


if __name__ == "__main__":
    asyncio.run(main())