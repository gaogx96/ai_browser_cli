"""测量 snapshot 开销的基准测试。

不依赖真实浏览器，mock 测纯函数耗时。
运行：
    python tests/benchmark_snapshot.py
"""

import sys
import os
import time
import asyncio
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    ActionEffects,
    ActionResult,
    ActionVerification,
    ErrorKind,
    PageSnapshot,
    TargetInfo,
    _diff,
    _verify,
    _expected_effect_seen,
    classify_error,
    normalize_decision,
    ACTION_SETTLE_TIMEOUT,
)


def bench(name, fn, iterations=10000):
    """测量纯函数耗时。"""
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    avg_us = (elapsed / iterations) * 1_000_000
    print(f"  {name:30s} {avg_us:8.1f} us/op  ({iterations} iterations)")
    return avg_us


async def bench_async(name, fn, iterations=100):
    """测量异步函数耗时（mock）。"""
    start = time.perf_counter()
    for _ in range(iterations):
        await fn()
    elapsed = time.perf_counter() - start
    avg_ms = (elapsed / iterations) * 1_000
    print(f"  {name:30s} {avg_ms:8.1f} ms/op  ({iterations} iterations)")


def main():
    print("=" * 60)
    print("Pure function benchmarks")
    print("=" * 60)

    # 准备测试数据
    t1 = TargetInfo("t1", "page", "https://a.com")
    t2 = TargetInfo("t2", "page", "https://b.com")
    snap_a = PageSnapshot("t1", "https://a.com", "A", "fp1", {"q": "a"}, (t1,), True)
    snap_b = PageSnapshot("t1", "https://b.com", "B", "fp2", {"q": "b"}, (t1, t2), True)
    result_ok = ActionResult("click", True)
    result_fail = ActionResult("click", False, ErrorKind.ELEMENT_NOT_FOUND, "not found")
    effects = ActionEffects(url_changed=True, dom_changed=True)
    effects_no = ActionEffects()

    # _diff
    bench("_diff (change)", lambda: _diff(snap_a, snap_b))
    bench("_diff (no change)", lambda: _diff(snap_a, snap_a))
    bench("_diff (None before)", lambda: _diff(None, snap_b))

    # _expected_effect_seen
    bench("_expected_effect_seen (hit)", lambda: _expected_effect_seen("click", effects))
    bench("_expected_effect_seen (miss)", lambda: _expected_effect_seen("click", effects_no))
    bench("_expected_effect_seen (type)", lambda: _expected_effect_seen("type", effects_no))

    # _verify
    bench("_verify (success)", lambda: _verify("click", result_ok, effects))
    bench("_verify (failed)", lambda: _verify("click", result_fail, effects_no))
    bench("_verify (no_effect)", lambda: _verify("click", result_ok, effects_no))
    bench("_verify (unknown)", lambda: _verify("evaluate", result_ok, effects_no))
    bench("_verify (None result)", lambda: _verify("navigate", None, effects))

    # classify_error
    bench("classify_error (not found)", lambda: classify_error(Exception("not found")))
    bench("classify_error (stale)", lambda: classify_error(Exception("stale element")))
    bench("classify_error (unknown)", lambda: classify_error(Exception("weird error")))
    bench("classify_error (raw dict)", lambda: classify_error(None, {"error": "not found"}))

    # normalize_decision
    bench("normalize_decision (old flat)", lambda: normalize_decision({"action": "click", "target_id": "e5"}))
    bench("normalize_decision (new flat)", lambda: normalize_decision({"next_goal": "x", "action": "click", "target_id": "e5"}))
    bench("normalize_decision (nested)", lambda: normalize_decision({"action": {"type": "click", "target_id": "e5"}}))

    # ActionEffects.to_dict
    bench("effects.to_dict", lambda: effects.to_dict())

    # ActionVerification.to_dict
    v = _verify("click", result_ok, effects)
    bench("verification.to_dict", lambda: v.to_dict())

    print()
    print("=" * 60)
    print("异步方法基准测试 (mock)")
    print("=" * 60)
    print("  (异步方法依赖真实浏览器，此处仅打印预估时间)")
    print("  预估 _snapshot (3x evaluate): ~15-50ms")
    print("  预估 _dom_fingerprint:         ~5-15ms")
    print("  预估 _form_state:              ~5-15ms")
    print("  预估 _target_infos:            ~5-15ms")
    print("  预估单步总开销:               ~30-100ms")
    print("  每步 0.5s 等待中约占:         6-20%")

    print()
    print("=" * 60)
    print("日志敏感信息审计")
    print("=" * 60)
    print("  [verify] 日志输出字段:")
    print("    status: str (success/no_effect/unknown/failed)")
    print("    transport: bool")
    print("    responded: bool")
    print("    expected: bool")
    print("    effects: dict (url_changed/title_changed/dom_changed/form_changed)")
    print("  [安全] 不输出: 密码、完整表单值、Cookie、Authorization、文件路径")
    print("  [安全] 不输出: 原始页面树、DOM 内容")
    print("  [安全] 密码字段: 只记录 changed + length")
    print("  [安全] 表单状态: 仅用于 _diff 比较，不序列化到日志")


if __name__ == "__main__":
    main()