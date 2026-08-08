"""API 健康检查脚本（公共接口，不调用私有方法）。

运行：
    export LLM_PROVIDER=deepseek
    export DEEPSEEK_API_KEY=sk-xxx
    export DEEPSEEK_MODEL=deepseek-v4-flash
    python tests/check_api.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    provider = os.environ.get("LLM_PROVIDER", "")
    if not provider:
        print("请设置 LLM_PROVIDER 环境变量（deepseek 或 anthropic 或 openai）")
        sys.exit(1)

    from llm import LLMClient

    try:
        llm = LLMClient(provider=provider)
        # 通过公共 decide 接口，用最小参数验证 API 连通性
        # 使用一个简单的 task 和空的 tree/history
        result = await llm.decide(
            task="测试连通性，仅回复 OK",
            tree="[@e1] button \"OK\"",
            meta={"url": "https://example.com", "title": "Test", "interactiveCount": 1},
            history=[],
        )
        action = result.get("action", "?")
        thought = result.get("thought", "")[:30]
        print(f"API OK (action={action}, thought={thought})")
        return True
    except Exception as e:
        print(f"API FAIL: {str(e)[:200]}")
        return False


if __name__ == "__main__":
    asyncio.run(main())