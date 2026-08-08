"""
Agent Runner — 浏览器自主规划 Agent。

核心逻辑：
1. 观察：调 page_tree / meta 获取当前页面状态
2. 决策：把任务 + 页面树 + 历史发给 LLM，LLM 返回下一步动作
3. 执行：根据 LLM 返回的动作调 BrowserClient
4. 记录：动作结果入历史，供下一步参考
5. 循环直到 LLM 输出 stop 或超步数
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from llm import LLMClient

# 日志输出到 stderr，避免污染 MCP 的 stdout JSON-RPC 协议
_log = lambda *a, **kw: print(*a, **kw, file=sys.stderr, flush=True)


# ── Agent 结果 ─────────────────────────────────────────────────────────────


class AgentResult:
    """Agent 执行结果。支持成功、失败、暂停三种状态。"""

    def __init__(self, success: bool, reason: str = "", steps: int = 0, history: list | None = None):
        self.success = success
        self.reason = reason
        self.steps = steps
        self.history = history or []
        self.status: str = "success" if success else "failed"
        self.checkpoint: "Checkpoint | None" = None

    @classmethod
    def paused(cls, checkpoint: "Checkpoint", reason: str = "用户介入") -> "AgentResult":
        r = cls(success=False, reason=reason, steps=checkpoint.step)
        r.status = "paused"
        r.checkpoint = checkpoint
        return r

    def __repr__(self) -> str:
        if self.status == "paused":
            return f"AgentResult(⏸ 暂停, 步数={self.steps}, 原因={self.reason})"
        status = "✅ 成功" if self.success else "❌ 失败"
        return f"AgentResult({status}, 步数={self.steps}, 原因={self.reason})"

    def to_dict(self) -> dict:
        d: dict = {
            "success": self.success,
            "status": self.status,
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
        if self.checkpoint:
            d["checkpoint"] = self.checkpoint.to_dict()
        return d


# ── 阶段 1：数据模型与纯函数 ─────────────────────────────────────────────
# 本阶段只新增结构和纯函数，不修改 run() 循环行为。
# 阶段 2（快照适配器）与阶段 3（shadow verification）在此基础上接入。


class ErrorKind(enum.Enum):
    """错误类型枚举。所有 _execute() 错误统一经 classify_error() 归类。"""

    ELEMENT_NOT_FOUND = "element_not_found"
    ELEMENT_NOT_INTERACTABLE = "element_not_interactable"
    NAVIGATION_TIMEOUT = "navigation_timeout"
    STALE_TARGET = "stale_target"
    PERMISSION_REQUIRED = "permission_required"
    UNKNOWN = "unknown"


# 动作执行结果（transport 层语义）
@dataclass
class ActionResult:
    action_type: str
    transport_ok: bool
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    duration_ms: int = 0
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "action_type": self.action_type,
            "transport_ok": self.transport_ok,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
        }


# 单个 target 信息（用于区分新标签页 / iframe / popup / 扩展页）
@dataclass(frozen=True)
class TargetInfo:
    target_id: str
    target_type: str  # page | iframe | popup | extension | other
    url: str = ""
    opener_id: str | None = None
    window_id: int | None = None


# 动作执行前的页面快照（baseline）
@dataclass
class PageSnapshot:
    target_id: str
    url: str
    title: str
    dom_fingerprint: str | None  # 可交互元素归一化指纹
    form_state: dict  # 输入框 value（密码脱敏）
    focused_element: dict | None = None  # 当前焦点元素 {tag, id, name, role, type}
    targets: tuple[TargetInfo, ...] = ()
    snapshot_ok: bool = True  # 快照是否成功（失败时其他字段可能为空）

    @property
    def target_ids(self) -> set[str]:
        return {t.target_id for t in self.targets}


# 前后快照差异
@dataclass
class ActionEffects:
    url_changed: bool = False
    title_changed: bool = False
    dom_changed: bool = False
    form_changed: bool = False
    focus_changed: bool = False
    new_targets: list[TargetInfo] = field(default_factory=list)
    closed_targets: list[TargetInfo] = field(default_factory=list)

    def any_change(self) -> bool:
        return any(
            [
                self.url_changed,
                self.title_changed,
                self.dom_changed,
                self.form_changed,
                self.focus_changed,
                bool(self.new_targets),
                bool(self.closed_targets),
            ]
        )

    def to_dict(self) -> dict:
        return {
            "url_changed": self.url_changed,
            "title_changed": self.title_changed,
            "dom_changed": self.dom_changed,
            "form_changed": self.form_changed,
            "focus_changed": self.focus_changed,
            "new_targets": [t.target_id for t in self.new_targets],
            "closed_targets": [t.target_id for t in self.closed_targets],
        }


# 动作通用验证结果（四态：success / no_effect / unknown / failed）
@dataclass
class ActionVerification:
    transport_ok: bool
    page_responded: bool
    expected_effect_seen: bool
    status: str  # success | no_effect | unknown | failed
    error_kind: ErrorKind | None = None
    needs_reobserve: bool = False

    def to_dict(self) -> dict:
        return {
            "transport_ok": self.transport_ok,
            "page_responded": self.page_responded,
            "expected_effect_seen": self.expected_effect_seen,
            "status": self.status,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "needs_reobserve": self.needs_reobserve,
        }


# 动作类型 → 预期信号（用于判定 expected_effect_seen）
ACTION_EXPECTED_EFFECTS: dict[str, set[str]] = {
    "navigate": {"url_changed", "title_changed", "dom_changed"},
    "click": {"url_changed", "dom_changed", "focus_changed", "form_changed", "new_targets"},
    "type": {"form_changed"},
    "evaluate": set(),  # evaluate 结果由动作专属判断，不依赖通用信号
    "download_setup": set(),
    "download": {"new_targets", "dom_changed"},
    "stop": set(),
    "pause": set(),
}

# 无视觉/无 DOM 副作用的动作：无预期信号时判为 unknown，而非 no_effect
NON_VISUAL_ACTIONS: set[str] = {"evaluate", "download_setup"}

# 动作类型 → 默认 settle 等待毫秒数（阶段 2 用）
ACTION_SETTLE_TIMEOUT: dict[str, int] = {
    "navigate": 3000,
    "click": 1500,
    "type": 500,
    "evaluate": 300,
    "download_setup": 200,
    "download": 2000,
    "stop": 0,
    "pause": 0,
}


def _classify_msg(msg: str) -> ErrorKind:
    """根据错误文本归类错误类型（exc 和 raw_result 共用）。"""
    if "not found" in msg or "not exist" in msg or "not in any frame" in msg:
        return ErrorKind.ELEMENT_NOT_FOUND
    if "stale" in msg or "detached" in msg or "already disposed" in msg or "disposed" in msg:
        return ErrorKind.STALE_TARGET
    if "timeout" in msg or "timed out" in msg:
        return ErrorKind.NAVIGATION_TIMEOUT
    if "not interactable" in msg or "not clickable" in msg or "covered" in msg:
        return ErrorKind.ELEMENT_NOT_INTERACTABLE
    if "permission" in msg or "login" in msg or "captcha" in msg or "风控" in msg:
        return ErrorKind.PERMISSION_REQUIRED
    return ErrorKind.UNKNOWN


def classify_error(exc: Exception | None, raw_result: dict | None = None) -> ErrorKind:
    """把异常/原始结果统一归类为 ErrorKind。所有 _execute() 错误都经过此入口。"""
    if exc is not None:
        return _classify_msg(str(exc).lower())

    if raw_result:
        err = str(raw_result.get("error", "")).lower()
        return _classify_msg(err)

    return ErrorKind.UNKNOWN


def _diff(before: PageSnapshot | None, after: PageSnapshot | None) -> ActionEffects:
    """计算前后快照的差异。任一侧快照失败时不误报变化。"""
    effects = ActionEffects()

    if before is None or after is None:
        return effects
    if not before.snapshot_ok or not after.snapshot_ok:
        return effects

    effects.url_changed = (before.url or "") != (after.url or "")
    effects.title_changed = (before.title or "") != (after.title or "")
    effects.dom_changed = (before.dom_fingerprint or "") != (after.dom_fingerprint or "")
    effects.form_changed = before.form_state != after.form_state
    effects.focus_changed = (before.focused_element or {}) != (after.focused_element or {})

    before_ids = before.target_ids
    after_ids = after.target_ids
    effects.new_targets = [t for t in after.targets if t.target_id not in before_ids]
    effects.closed_targets = [t for t in before.targets if t.target_id not in after_ids]

    return effects


def _expected_effect_seen(action_type: str, effects: ActionEffects) -> bool:
    """判断动作类型的预期信号是否出现。"""
    expected = ACTION_EXPECTED_EFFECTS.get(action_type, set())
    if not expected:
        return False
    return any(
        (
            "url_changed" in expected and effects.url_changed,
            "title_changed" in expected and effects.title_changed,
            "dom_changed" in expected and effects.dom_changed,
            "form_changed" in expected and effects.form_changed,
            "focus_changed" in expected and effects.focus_changed,
            "new_targets" in expected and bool(effects.new_targets),
        )
    )


def _verify(
    action_type: str,
    result: ActionResult | None,
    effects: ActionEffects | None,
) -> ActionVerification:
    """通用验证：解释动作执行结果。只判 transport 与页面响应，不判目标是否完成。"""
    effects = effects or ActionEffects()

    if result is not None and not result.transport_ok:
        return ActionVerification(
            transport_ok=False,
            page_responded=False,
            expected_effect_seen=False,
            status="failed",
            error_kind=result.error_kind,
            needs_reobserve=True,
        )

    transport_ok = result is None or result.transport_ok
    page_responded = effects.any_change()
    expected = _expected_effect_seen(action_type, effects)

    if expected:
        status = "success"
    elif action_type in NON_VISUAL_ACTIONS:
        status = "unknown"
    else:
        status = "no_effect"

    return ActionVerification(
        transport_ok=transport_ok,
        page_responded=page_responded,
        expected_effect_seen=expected,
        status=status,
    )


# LLM 决策归一化：兼容三种输出格式，统一为 Decision
@dataclass
class Decision:
    action_type: str
    target_id: str = ""
    url: str = ""
    text: str = ""
    expression: str = ""
    path: str = ""
    reason: str = ""
    next_goal: str = ""
    evaluation_previous_goal: str = ""
    memory: str = ""
    is_pause: bool = False
    operation: str = ""  # C3-2：结构化操作（focus/set_value 等）

    def to_action_dict(self) -> dict:
        """转回 AgentRunner 现有 _execute() 期望的扁平动作 dict。"""
        d: dict[str, Any] = {"action": self.action_type}
        if self.target_id:
            d["target_id"] = self.target_id
        if self.url:
            d["url"] = self.url
        if self.text:
            d["text"] = self.text
        if self.expression:
            d["expression"] = self.expression
        if self.path:
            d["path"] = self.path
        if self.reason:
            d["reason"] = self.reason
        if self.operation:
            d["operation"] = self.operation
        return d


# ── C3-2：结构化 Evaluate ────────────────────────────────────────────────
# 不依赖 LLM 生成任意 JavaScript。结构化操作由框架生成固定脚本。


class EvaluateOperation(str, enum.Enum):
    FOCUS = "focus"
    SET_VALUE = "set_value"
    SCROLL_INTO_VIEW = "scroll_into_view"
    DISPATCH_INPUT = "dispatch_input"
    DISPATCH_CHANGE = "dispatch_change"
    READ_PROPERTY = "read_property"


@dataclass
class EvaluateRequest:
    operation: str
    target_id: str | None = None
    value: str | None = None
    property_name: str | None = None


def _js_quote(s: str) -> str:
    """安全转义 JS 字符串，防止引号/换行/Unicode 破坏脚本。"""
    return json.dumps(s, ensure_ascii=False)


def generate_script(req: EvaluateRequest) -> str:
    """根据结构化操作生成固定脚本。参数通过安全序列化注入。"""
    op = req.operation

    if op == EvaluateOperation.FOCUS:
        return r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return {ok: false, reason: 'not_found'};
          el.focus();
          return {ok: true, focused: document.activeElement === el};
        })()
        """ % req.target_id.replace('"', '\\"')

    if op == EvaluateOperation.SET_VALUE:
        # 设置 value 并派发 input/change 事件。
        # 直接赋值 + dispatchEvent 在大多数框架（包括 React/Vue）中有效。
        # 注意：某些浏览器中 Object.getOwnPropertyDescriptor 的 setter
        # 会抛出 'Illegal invocation'，因此优先使用直接赋值 + 事件派发。
        return r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return {ok: false, reason: 'not_found'};
          const nativeSetter = Object.getOwnPropertyDescriptor(
            (el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement : window.HTMLInputElement).prototype, 'value'
          )?.set;
          if (nativeSetter) {
            nativeSetter.call(el, %s);
          } else {
            el.value = %s;
          }
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return {ok: true, value_set: el.value === %s};
        })()
        """ % (req.target_id.replace('"', '\\"'), _js_quote(req.value or ""),
               _js_quote(req.value or ""), _js_quote(req.value or ""))

    if op == EvaluateOperation.SCROLL_INTO_VIEW:
        return r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return {ok: false, reason: 'not_found'};
          el.scrollIntoView({behavior: 'smooth', block: 'center'});
          return {ok: true};
        })()
        """ % req.target_id.replace('"', '\\"')

    if op == EvaluateOperation.DISPATCH_INPUT:
        return r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return {ok: false, reason: 'not_found'};
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return {ok: true};
        })()
        """ % req.target_id.replace('"', '\\"')

    if op == EvaluateOperation.DISPATCH_CHANGE:
        return r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return {ok: false, reason: 'not_found'};
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return {ok: true};
        })()
        """ % req.target_id.replace('"', '\\"')

    if op == EvaluateOperation.READ_PROPERTY:
        prop = req.property_name or "value"
        return r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return {ok: false, reason: 'not_found'};
          const v = el[%s];
          if (typeof v === 'string') return {ok: true, value: v.length > 100 ? v.substring(0, 100) : v, truncated: v.length > 100};
          return {ok: true, value: v};
        })()
        """ % (req.target_id.replace('"', '\\"'), _js_quote(prop))

    return r"(() => { return {ok: false, reason: 'unknown_operation'}; })()"


def normalize_evaluate_operation(action: dict) -> EvaluateRequest | None:
    """从 Decision 中提取结构化 evaluate 操作。

    支持两种格式：
    - {"action": "evaluate", "operation": "focus", "target_id": "e5"}
    - {"action": "focus", "target_id": "e5"}  (直接结构化动作)
    """
    op = action.get("operation", "")
    if op:
        return EvaluateRequest(
            operation=op,
            target_id=action.get("target_id", ""),
            value=action.get("value", ""),
            property_name=action.get("property_name", ""),
        )
    return None


# ── 结构化操作白名单（可由 LLM 直接输出为 action） ─────────────────────
STRUCTURED_EVALUATE_ACTIONS = {
    "focus", "set_value", "scroll_into_view", "dispatch_input", "dispatch_change", "read_property",
}


def normalize_decision(raw: dict) -> Decision:
    """把 LLM 输出归一化为 Decision，兼容旧版/新版扁平/新版嵌套三种格式。

    统一入口，run() 不再直接处理三种格式。
    """
    action = raw.get("action", "stop")

    # 嵌套格式: {"action": {"type": "click", "target_id": "e5"}}
    if isinstance(action, dict):
        action_type = action.get("type", "") or action.get("action", "")
        target_id = action.get("target_id", "")
        url = action.get("url", "")
        text = action.get("text", "")
        expression = action.get("expression", "")
        path = action.get("path", "")
        reason = action.get("reason", "")
        operation = action.get("operation", "")
    # 扁平格式: {"action": "click", "target_id": "e5"}
    else:
        action_type = str(action)
        target_id = raw.get("target_id", "")
        url = raw.get("url", "")
        text = raw.get("text", "")
        expression = raw.get("expression", "")
        path = raw.get("path", "")
        reason = raw.get("reason", "")
        operation = raw.get("operation", "")

    return Decision(
        action_type=action_type,
        target_id=str(target_id),
        url=str(url),
        text=str(text),
        expression=str(expression),
        path=str(path),
        reason=str(reason),
        next_goal=str(raw.get("next_goal", "")),
        evaluation_previous_goal=str(raw.get("evaluation_previous_goal", "")),
        memory=str(raw.get("memory", "")),
        is_pause=(action_type == "pause"),
        operation=str(operation),
    )


# ── 阶段 4：AgentState（滚动式目标状态机） ────────────────────────────────
# 职责：维护事实状态（current_goal / completed_goals / failed_attempts）。
# LLM 只提议 next_goal / evaluation_previous_goal，不直接修改状态。
# 本阶段只做双写（legacy history 保留），不启用自动恢复、不改变动作执行结果。


@dataclass
class AgentState:
    task: str
    current_goal: str | None = None
    next_goal: str | None = None
    previous_goal: str | None = None  # 上一目标，用于分析目标跳跃
    goal_status: str = "not_started"  # not_started | in_progress | completed | failed
    completed_goals: list[str] = field(default_factory=list)
    failed_attempts: list[str] = field(default_factory=list)
    last_observation_summary: str = ""
    step: int = 0
    session_id: str = ""  # P8-D：用于事件关联
    goal_transitions: list[dict] = field(default_factory=list)  # P8-D：目标推进记录
    blocked_targets: dict[str, str] = field(default_factory=dict)  # target_id → reason（C3-3：供 LLM 上下文）

    @staticmethod
    def _normalize_goal(goal: str | None) -> str:
        """规范化目标文本，用于比较。"""
        if not goal:
            return ""
        return goal.strip().lower().rstrip("。，.!.")

    @staticmethod
    def _evaluation_says_completed(eval_text: str | None) -> bool:
        """判断 LLM 的 evaluation_previous_goal 是否表示目标已完成。"""
        if not eval_text:
            return False
        text = eval_text.strip().lower()
        completed_keywords = ["已完成", "完成", "completed", "done", "成功", "succeeded"]
        return any(kw in text for kw in completed_keywords)

    # 观察页面并更新最近观察摘要
    def observe(self, tree: str, meta: dict) -> None:
        self.step += 1
        title = meta.get("title", "")
        url = meta.get("url", "")
        interactive = meta.get("interactiveCount", 0)
        # 摘要把页面树截断，避免把完整 DOM 塞入 LLM 上下文
        tree_lines = [l for l in tree.split("\n") if l.strip()]
        summary_lines = tree_lines[:8]
        truncated = "…" if len(tree_lines) > 8 else ""
        self.last_observation_summary = (
            f"[step {self.step}] url={url} title={title} interactive={interactive}\n"
            + "\n".join(summary_lines)
            + truncated
        )

    # 记录 LLM 提议的下一个目标（runtime 采纳，非直接覆盖）
    def record_goal_proposal(self, decision: "Decision") -> None:
        """根据 LLM 提议推进目标。

        规则：
        - current_goal 为空 → 直接设置 next_goal
        - next_goal 与 current_goal 相同 → 不变
        - LLM 明确表示上一目标已完成 → 晋升 next_goal 为 current_goal
        - LLM 提出不同 next_goal 但未说明完成 → 暂存，不覆盖
        """
        next_goal = decision.next_goal
        if not next_goal:
            return

        self.next_goal = next_goal
        norm_next = self._normalize_goal(next_goal)
        norm_cur = self._normalize_goal(self.current_goal)

        if self.current_goal is None:
            self.current_goal = next_goal
            self.goal_status = "in_progress"
            # 首次设置时，next_goal 与 current_goal 相同，不置空
            self.goal_transitions.append({
                "from": None, "to": next_goal,
                "reason": "initial",
                "step": self.step,
            })
            return

        if norm_next == norm_cur:
            self.next_goal = next_goal
            return

        # 去重检查：同一规范化目标在最近 5 步内出现过 → 标记重复
        deduplicated = False
        recent_goals = [t["to"] for t in self.goal_transitions[-5:]]
        for recent in recent_goals:
            if self._normalize_goal(recent) == norm_next:
                deduplicated = True
                break

        # 检查 LLM 是否表示上一目标已完成
        eval_text = decision.evaluation_previous_goal
        if self._evaluation_says_completed(eval_text):
            # 推进目标（去重检查）
            self.previous_goal = self.current_goal
            self.current_goal = next_goal
            self.next_goal = None
            self.goal_status = "in_progress"
            self.goal_transitions.append({
                "from": self.previous_goal,
                "to": next_goal,
                "reason": f"evaluation: {eval_text[:50]}",
                "step": self.step,
                "deduplicated": deduplicated,
            })
        else:
            # 暂存为 next_goal，不覆盖 current_goal
            self.next_goal = next_goal

    # 记录一次失败尝试（仅明确失败时）
    def record_failure(self, decision: "Decision", error: str) -> None:
        self.failed_attempts.append(
            f"step{self.step}:{decision.action_type}:{error[:80]}"
        )
        if len(self.failed_attempts) > 5:
            self.failed_attempts = self.failed_attempts[-5:]

    # 标记当前目标完成。仅接受明确事实（如显式 stop 成功），
    # 不因 verification.status == "success" 就自动标记完成。
    def mark_goal_completed(self, goal: str | None = None) -> None:
        g = goal or self.current_goal or self.next_goal
        if g and g not in self.completed_goals:
            self.completed_goals.append(g)
        if len(self.completed_goals) > 10:
            self.completed_goals = self.completed_goals[-10:]
        self.current_goal = None
        self.goal_status = "completed"

    # 生成结构化上下文（供 build_context 使用）
    def build_context(self, observation_summary: str | None = None) -> dict:
        return {
            "task": self.task,
            "current_goal": self.current_goal,
            "next_goal": self.next_goal,
            "goal_status": self.goal_status,
            "completed_goals": self.completed_goals,
            "failed_attempts": self.failed_attempts,
            "last_observation": observation_summary or self.last_observation_summary,
        }

    # 把结构化上下文格式化为 LLM prompt 文本
    def build_context_text(self) -> str:
        lines = [
            f"任务: {self.task}",
            f"当前目标: {self.current_goal or '（未开始）'}",
            f"下一目标: {self.next_goal or '（未提议）'}",
            f"目标状态: {self.goal_status}",
        ]
        if self.completed_goals:
            lines.append("已完成: " + " | ".join(self.completed_goals))
        if self.failed_attempts:
            lines.append("最近失败: " + " | ".join(self.failed_attempts))
        if self.blocked_targets:
            blocked_lines = []
            for tid, reason in self.blocked_targets.items():
                blocked_lines.append(f"{tid}: {reason}")
            lines.append("不可用目标: " + " | ".join(blocked_lines))
        lines.append("最近观察:")
        lines.append(self.last_observation_summary)
        return "\n".join(lines)


# ── 阶段 5：恢复决策 ──────────────────────────────────────────────────────
# 注意：reobserve 后不自动重放旧动作，回到 LLM 重新决策。
# 只有 NAVIGATION_TIMEOUT + URL 未变化时允许有限重试一次。


@dataclass
class RecoveryDecision:
    kind: str  # none | reobserve | retry | abort
    reason: str
    retry_action: bool = False
    force_reobserve: bool = False
    max_attempts: int = 0


# ── 阶段 6A：Pause / Resume ────────────────────────────────────────────────
# 同一进程内 checkpoint + 暂停/恢复。恢复后必须重新观察页面，
# 不复用旧 target_id 或旧 action。跨进程持久化暂不实现。


class PauseReason(str, enum.Enum):
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_PAGE = "waiting_for_page"
    WAITING_FOR_EXTERNAL_EVENT = "waiting_for_external_event"


@dataclass
class Checkpoint:
    version: int = 1
    checkpoint_id: str = ""
    session_id: str = ""
    task: str = ""
    current_goal: str | None = None
    next_goal: str | None = None
    completed_goals: list[str] = field(default_factory=list)
    failed_attempts: list[str] = field(default_factory=list)
    step: int = 0
    page_url: str = ""
    page_fingerprint: str | None = None
    pause_reason: str = "waiting_for_user"
    snapshot_available: bool = True

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "task": self.task,
            "current_goal": self.current_goal,
            "next_goal": self.next_goal,
            "completed_goals": self.completed_goals,
            "failed_attempts": self.failed_attempts,
            "step": self.step,
            "page_url": self.page_url,
            "pause_reason": self.pause_reason,
            "snapshot_available": self.snapshot_available,
        }


# 页面匹配等级（用于 resume 判断）
class PageMatchLevel(enum.Enum):
    STRONG = "strong"  # URL 相同 + 关键元素存在
    WEAK = "weak"      # URL 属于同一流程
    NONE = "none"      # URL 完全不同 / 错误页


# ── P8-C：GoalAssessment ──────────────────────────────────────────────────
# 数据模型和纯函数。先 shadow 只记录，不改变行为。


@dataclass
class GoalEvidence:
    """目标完成的证据。"""
    kind: str  # url_match | element_present | text_present | download_detected | form_state | llm_judgment
    detail: str  # 具体描述（如 "URL 到达目标页面 https://example.com"）
    weight: float = 1.0  # 证据权重


@dataclass
class GoalAssessment:
    """目标完成评估结果。

    status:
      completed  — 有足够证据认为目标已完成
      partial    — 目标只完成一部分
      failed     — 页面或动作明确表明目标失败
      unknown    — 当前证据不足，不能判断
    """
    goal: str | None = None
    status: str = "unknown"
    confidence: float = 0.0
    evidence: list[GoalEvidence] = field(default_factory=list)
    source: str = "unknown"  # rule | llm | combined
    stable: bool = False  # 是否连续多次评估一致
    required_confirmation: bool = False  # 是否需要用户确认

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "status": self.status,
            "confidence": round(self.confidence, 2),
            "evidence_count": len(self.evidence),
            "source": self.source,
            "stable": self.stable,
            "required_confirmation": self.required_confirmation,
        }


# 显式规则证据：根据动作类型和页面状态生成
def assess_from_rules(action_type: str, before: PageSnapshot | None, after: PageSnapshot | None,
                      effects: ActionEffects | None, current_goal: str | None) -> GoalAssessment:
    """根据显式规则评估目标是否完成。不依赖 LLM，纯规则判断。

    规则覆盖：
    - navigate: URL 到达目标
    - click: URL 变化 / 新标签页 / DOM 变化
    - type: 表单状态变化
    """
    effects = effects or ActionEffects()
    evidence: list[GoalEvidence] = []
    status = "unknown"
    confidence = 0.0

    if action_type == "navigate":
        if effects.url_changed:
            evidence.append(GoalEvidence("url_match", "URL 已发生变化", weight=0.9))
        if effects.title_changed:
            evidence.append(GoalEvidence("text_present", "页面标题已变化", weight=0.5))
        if evidence:
            status = "completed"
            confidence = min(0.5 + 0.4 * len(evidence), 0.95)

    elif action_type == "click":
        if effects.url_changed:
            evidence.append(GoalEvidence("url_match", "点击后 URL 已变化", weight=0.8))
        if effects.new_targets:
            evidence.append(GoalEvidence("element_present", "新标签页已打开", weight=0.9))
        if effects.dom_changed:
            evidence.append(GoalEvidence("element_present", "页面 DOM 已变化", weight=0.5))
        if effects.form_changed:
            evidence.append(GoalEvidence("form_state", "表单状态已变化", weight=0.7))
        # focus_changed 是辅助信号（浏览器 click 不保证 focus），
        # 不能单独作为完成证据；只在有其他强证据时作为补充。
        focus_aux = effects.focus_changed
        if evidence:
            status = "completed"
            confidence = min(0.3 + 0.5 * len(evidence), 0.9)
            if focus_aux:
                evidence.append(GoalEvidence("focus_received", "页面焦点已变化（辅助信号）", weight=0.2))
        elif focus_aux:
            # 仅焦点变化 → partial，不判 completed
            status = "partial"
            confidence = 0.4
            evidence.append(GoalEvidence("focus_received", "仅焦点变化（辅助信号）", weight=0.2))

    elif action_type == "type":
        if effects.form_changed:
            evidence.append(GoalEvidence("form_state", "表单状态已变化", weight=0.7))
            status = "completed"
            confidence = 0.7

    return GoalAssessment(
        goal=current_goal,
        status=status if evidence else "unknown",
        confidence=confidence,
        evidence=evidence,
        source="rule",
        stable=True,
    )


def merge_rule_and_llm_assessment(
    rule_assessment: GoalAssessment,
    llm_assessment: GoalAssessment | None,
) -> GoalAssessment:
    """合并规则评估和 LLM 评估。规则证据优先于 LLM 判断。"""
    if not llm_assessment or llm_assessment.status == "unknown":
        # 规则有证据但 LLM 不确认 → 仍需确认
        if rule_assessment.status == "completed":
            return GoalAssessment(
                goal=rule_assessment.goal,
                status="completed",
                confidence=rule_assessment.confidence * 0.8,
                evidence=rule_assessment.evidence,
                source=rule_assessment.source,
                stable=True,
                required_confirmation=True,
            )
        return rule_assessment

    if rule_assessment.status == "completed" and llm_assessment.status == "completed":
        # 双方一致 → 高置信度
        evidence = rule_assessment.evidence + llm_assessment.evidence
        confidence = min(rule_assessment.confidence + llm_assessment.confidence * 0.3, 0.99)
        return GoalAssessment(
            goal=rule_assessment.goal or llm_assessment.goal,
            status="completed",
            confidence=confidence,
            evidence=evidence,
            source="combined",
            stable=True,
        )

    if rule_assessment.status == "completed" and llm_assessment.status != "completed":
        # 规则证据明确，但 LLM 不确认 → 仍接受规则判断
        return GoalAssessment(
            goal=rule_assessment.goal,
            status="completed",
            confidence=rule_assessment.confidence * 0.8,
            evidence=rule_assessment.evidence,
            source="rule",
            stable=True,
            required_confirmation=True,
        )

    if rule_assessment.status == "unknown" and llm_assessment.status == "completed":
        # 规则无证据，但 LLM 认为完成 → 低置信度，标记为 partial
        return GoalAssessment(
            goal=llm_assessment.goal,
            status="partial",
            confidence=llm_assessment.confidence * 0.5,
            evidence=llm_assessment.evidence,
            source="llm",
            stable=False,
            required_confirmation=True,
        )

    # 双方都 unknown → 保持 unknown
    return rule_assessment


def should_accept_completion(assessment: GoalAssessment) -> bool:
    """判断是否应接受目标完成评估。

    规则证据明确 + 高置信度 → 接受
    LLM-only + 低置信度 → 不自动接受
    """
    if assessment.status != "completed":
        return False
    if assessment.source == "rule" and assessment.confidence >= 0.7:
        return True
    if assessment.source == "combined" and assessment.confidence >= 0.85:
        return True
    if assessment.source == "llm" and assessment.confidence >= 0.9 and assessment.stable:
        return True
    return False


# ── C3-1：TargetValidation ────────────────────────────────────────────────
# 动作前 target 验证。三态结果：valid / invalid / unknown。
# unknown 不阻断（CDP 页面导航时无法可靠读取元素状态）。


@dataclass
class TargetValidation:
    status: str  # valid | invalid | unknown
    target_id: str
    action_type: str
    reason: str | None = None
    tag: str | None = None
    role: str | None = None
    visible: bool | None = None
    enabled: bool | None = None
    connected: bool | None = None


# 动作类型 → 允许的 tag/role 集合
ALLOWED_TAGS_FOR_TYPE: set[str] = {"input", "textarea"}
ALLOWED_ROLES_FOR_TYPE: set[str] = {"textbox", "searchbox", "combobox", "contenteditable"}
ALLOWED_TAGS_FOR_CLICK: set[str] = {
    "a", "button", "input", "select", "textarea", "summary", "details", "label",
}
ALLOWED_ROLES_FOR_CLICK: set[str] = {
    "button", "link", "checkbox", "radio", "tab", "menuitem", "option",
    "switch", "combobox", "spinbutton", "slider", "searchbox", "textbox",
}

# 排除的 type 值（用于 type 动作）
EXCLUDED_TYPES_FOR_TYPE: set[str] = {"button", "submit", "checkbox", "radio", "file", "hidden"}


def validate_target(target_id: str, action_type: str, element_info: dict | None) -> TargetValidation:
    """验证目标元素是否适合执行指定动作。

    三态：
    - valid: 明确可以执行
    - invalid: 明确不能执行（如 type + button）
    - unknown: 无法可靠确认（如页面正在导航）

    注意：不要过度拦截（如普通 div + click 应允许）。
    """
    if not target_id:
        return TargetValidation("invalid", target_id, action_type, reason="target_id 为空")

    if element_info is None:
        return TargetValidation("unknown", target_id, action_type, reason="无法读取元素信息")

    tag = (element_info.get("tag") or "").lower()
    role = (element_info.get("role") or "").lower()
    elem_type = (element_info.get("type") or "").lower()
    visible = element_info.get("visible", False)
    enabled = element_info.get("enabled", True)
    connected = element_info.get("connected", True)

    if not connected:
        return TargetValidation("invalid", target_id, action_type, reason="元素已脱离 DOM",
                                tag=tag, role=role, connected=False)

    if not visible:
        return TargetValidation("invalid", target_id, action_type, reason="元素不可见",
                                tag=tag, role=role, visible=False)

    if action_type == "type":
        return _validate_type_target(target_id, tag, role, elem_type, enabled, element_info)

    if action_type == "click":
        return _validate_click_target(target_id, tag, role, elem_type, enabled, element_info)

    return TargetValidation("valid", target_id, action_type, reason="无需验证")


def _validate_type_target(target_id: str, tag: str, role: str, elem_type: str,
                          enabled: bool, info: dict) -> TargetValidation:
    """验证 type 动作的目标。"""
    # 明确排除：button/submit/checkbox/radio/file/hidden
    if elem_type in EXCLUDED_TYPES_FOR_TYPE:
        return TargetValidation("invalid", target_id, "type",
                                reason=f"type 目标不能是 input[{elem_type}]",
                                tag=tag, role=role)

    if tag == "input":
        if not enabled:
            return TargetValidation("invalid", target_id, "type", reason="输入框已禁用",
                                    tag=tag, role=role, enabled=False)
        return TargetValidation("valid", target_id, "type", reason="input 输入框", tag=tag, role=role)

    if tag == "textarea":
        if not enabled:
            return TargetValidation("invalid", target_id, "type", reason="文本域已禁用",
                                    tag=tag, role=role, enabled=False)
        return TargetValidation("valid", target_id, "type", reason="textarea 文本域", tag=tag, role=role)

    # contenteditable
    if info.get("contenteditable"):
        return TargetValidation("valid", target_id, "type", reason="contenteditable 元素", tag=tag, role=role)

    # role=textbox 但不满足以上条件 → 可疑但执行
    if role in ("textbox", "searchbox", "combobox"):
        return TargetValidation("valid", target_id, "type", reason=f"role={role} 元素", tag=tag, role=role)

    # 明确不匹配：type + button
    if tag == "button" or role == "button":
        return TargetValidation("invalid", target_id, "type",
                                reason=f"type 目标不能是 button",
                                tag=tag, role=role)

    # 其他情况 → unknown（不阻断）
    return TargetValidation("unknown", target_id, "type", reason=f"非标准输入元素 ({tag})",
                            tag=tag, role=role)


def _validate_click_target(target_id: str, tag: str, role: str, elem_type: str,
                           enabled: bool, info: dict) -> TargetValidation:
    """验证 click 动作的目标。"""
    if tag == "input" and elem_type in ("hidden",):
        return TargetValidation("invalid", target_id, "click", reason="hidden input 不可点击",
                                tag=tag, role=role, visible=False)

    if tag == "input" and elem_type in ("button", "submit", "checkbox", "radio", "file"):
        return TargetValidation("valid", target_id, "click", reason=f"input[{elem_type}] 可点击",
                                tag=tag, role=role)

    if tag in ALLOWED_TAGS_FOR_CLICK:
        return TargetValidation("valid", target_id, "click", reason=f"<{tag}> 可点击", tag=tag, role=role)

    if role in ALLOWED_ROLES_FOR_CLICK:
        return TargetValidation("valid", target_id, "click", reason=f"role={role} 可点击", tag=tag, role=role)

    # 普通 div/span 等 → unknown（不阻断，可能有 JS click handler）
    return TargetValidation("unknown", target_id, "click", reason=f"非标准交互元素 (<{tag}>)",
                            tag=tag, role=role)


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

        # 阶段 2-3：验证层配置
        self._verify_mode = os.environ.get("AGENT_VERIFY_MODE", "shadow").lower()
        # off | shadow | active — 默认 shadow 只记录不干预
        self._last_snapshot: PageSnapshot | None = None  # 前一步的 after snapshot

        # 阶段 4：AgentState 配置
        self._context_mode = os.environ.get("AGENT_CONTEXT_MODE", "dual").lower()
        # legacy | dual | structured — 默认 dual 双写，legacy 保留旧行为
        self.state: AgentState | None = None  # 由 run() 初始化

        # LLM 调用间延迟（秒），用于适应 API rpm 限制。0 = 不延迟
        self._llm_delay = float(os.environ.get("AGENT_LLM_DELAY", "0"))

        # 阶段 5：恢复层配置
        self._recovery_mode = os.environ.get("AGENT_RECOVERY_MODE", "off").lower()
        # off | shadow | active — 默认 off 保留旧逻辑
        self._recovery_retry_counts: dict[str, int] = {}  # 按 action_id 计数，非全局

        # P8-C：GoalAssessment 配置
        self._goal_assessment_mode = os.environ.get("AGENT_GOAL_ASSESSMENT", "off").lower()
        # off | shadow | active — 默认 off，shadow 只记录不干预

        # 循环防护配置（重复 no_effect 动作检测）
        self._loop_guard_mode = os.environ.get("AGENT_LOOP_GUARD", "off").lower()
        # off | shadow | active — 默认 off
        self._no_effect_counts: dict[str, int] = {}  # action_signature → 计数
        self._last_action_signature: str = ""  # 上一步的动作签名，用于重置

        # C3-1：动作前 target 验证配置
        self._action_guard_mode = os.environ.get("AGENT_ACTION_GUARD", "off").lower()
        # off | shadow | active — 默认 off
        # active 只拦截明确 invalid，unknown 放行

        # C3-2：raw evaluate 开关
        self._raw_evaluate_mode = os.environ.get("AGENT_RAW_EVALUATE", "off").lower()
        # off | shadow | active — 默认 off（拒绝 LLM 生成任意 JS）

        # P8-D：可观测性配置
        self._observability = os.environ.get("AGENT_OBSERVABILITY", "stderr").lower()
        # off | stderr | jsonl — 默认 stderr，jsonl 适合统计/误判分析
        self._observability_path = os.environ.get("AGENT_OBSERVABILITY_PATH", "")
        # jsonl 输出文件路径；为空则用 stderr
        self._event_count = 0

    async def run(self, task: str, initial_state: AgentState | None = None) -> AgentResult:
        """执行一个自然语言浏览器任务。

        initial_state: 可选的初始状态（用于 resume 恢复）。
        提供时跳过 state 初始化，保留调用方设置的状态。
        """
        self.history = []
        self.attempted = set()
        self._retry_counts = {}
        self._last_snapshot = None
        self.state = initial_state or AgentState(task=task)

        for step in range(1, self.max_steps + 1):
            _log(f"\n--- Step {step}/{self.max_steps} ---")

            # 1. 观察
            tree, meta = await self._observe()

            # 2. 阶段 4：更新 AgentState 观察
            self.state.observe(tree, meta)

            # 3. 决策（前：适应 API rpm 限制的延迟）
            if self._llm_delay > 0 and step > 1:
                _log(f"  等待 {self._llm_delay}s 适应 API 限流...")
                await asyncio.sleep(self._llm_delay)
            if self._context_mode == "legacy":
                # 旧模式：直接用 history 列表
                action = await self.llm.decide(task, tree, meta, self.history)
            else:
                # dual / structured 模式：用 AgentState 结构化上下文
                context_text = self.state.build_context_text()
                action = await self.llm.decide(task, tree, meta, self.history, extra_context=context_text)
            _log(f"  决策: {json.dumps(action, ensure_ascii=False)[:200]}")

            # 4. 归一化决策（兼容新旧格式）
            decision = normalize_decision(action)

            # 5. 阶段 4：记录 LLM 提议的目标
            if self._context_mode != "legacy":
                self.state.record_goal_proposal(decision)

            # 6. 终止判断
            if decision.action_type == "stop":
                reason = decision.reason or "Agent 主动停止"
                _log(f"  Agent 停止: {reason}")
                # 明确事实：stop 成功 → 标记目标完成
                if self._context_mode != "legacy":
                    self.state.mark_goal_completed()
                return AgentResult(
                    success=True,
                    reason=reason,
                    steps=step,
                    history=self.history,
                )

            # 6A. Pause 判断
            if decision.is_pause:
                pause_reason = decision.reason or "waiting_for_user"
                _log(f"  Agent 暂停: {pause_reason}")
                observation = (tree, meta)
                checkpoint = await self._create_checkpoint(
                    observation=observation,
                    decision=decision,
                    pause_reason=pause_reason,
                )
                return AgentResult.paused(checkpoint, reason=pause_reason)

            # 7. 阶段 3：shadow 验证前置快照（仅记录，不干预）
            if self._verify_mode != "off":
                before = await self._snapshot()
            else:
                before = None

            # 8. C3-1：动作前 target 验证（shadow / active）
            action_guard_result = await self._validate_action_target(decision)
            if (self._action_guard_mode == "active"
                    and action_guard_result is not None
                    and action_guard_result.status == "invalid"):
                # 明确无效目标：不执行，强制 reobserve，回到 LLM 重新决策
                _log(f"  [action_guard] 拦截无效目标: {action_guard_result.reason}")
                self.attempted.add(decision.target_id)
                if self.state and decision.target_id:
                    self.state.blocked_targets[decision.target_id] = f"invalid_target: {action_guard_result.reason}"
                    if len(self.state.blocked_targets) > 20:
                        self.state.blocked_targets = dict(list(self.state.blocked_targets.items())[-20:])
                history_entry = {
                    "step": step,
                    "action": decision.to_action_dict(),
                    "success": False,
                    "error": f"action_guard: {action_guard_result.reason}",
                }
                self.history.append(history_entry)
                continue

            # 9. 执行
            result = await self._execute(decision.to_action_dict())

            # 9. 阶段 3：shadow 验证后置快照 + diff + verify（仅记录）
            if self._verify_mode != "off":
                after = await self._snapshot_after_action(
                    decision.action_type,
                    settle_ms=0,  # run() 已有 0.5s 等待，避免重复等待
                )
                effects = _diff(before, after)
                action_result = self._wrap_result(decision.to_action_dict(), result)
                verification = _verify(
                    action_type=decision.action_type,
                    result=action_result,
                    effects=effects,
                )
                self._log_verification(verification, effects)

                # P8-C：GoalAssessment shadow（仅记录，不干预）
                if self._goal_assessment_mode != "off":
                    assessment = self._assess_goal(
                        action_type=decision.action_type,
                        before=before,
                        after=after,
                        effects=effects,
                    )
                    # C2 严格状态更新：仅 active 模式 + 高置信度规则证据
                    c2_applied = False
                    if self._goal_assessment_mode == "active" and self.state:
                        # C2 严格模式：不接受 LLM-only 判断
                        if assessment.source == "llm":
                            pass
                        elif should_accept_completion(assessment):
                            # 只更新 completed_goals，
                            # 不自动 stop，不跳过 LLM，不触发 recovery
                            goal_key = self.state._normalize_goal(assessment.goal or "")
                            norm_current = self.state._normalize_goal(self.state.current_goal)
                            if goal_key and goal_key == norm_current:
                                self.state.mark_goal_completed(assessment.goal)
                                c2_applied = True
                                _log(f"  [c2] 目标完成: {assessment.goal}")

                    self._log_assessment(
                        assessment, applied=c2_applied,
                        action_type=decision.action_type,
                    )
            else:
                after = None
                verification = None

            # 10. 记录（legacy history 双写）
            history_entry = {
                "step": step,
                "action": decision.to_action_dict(),
                "success": result.get("success", False),
                "error": result.get("error", ""),
            }
            self.history.append(history_entry)

            # 11. 阶段 4：记录失败
            if not result.get("success") and self._context_mode != "legacy":
                self.state.record_failure(decision, str(result.get("error", "")))

            if result.get("success"):
                _log(f"  执行成功")
            else:
                _log(f"  执行失败: {result.get('error', '')}")

            # 12. 阶段 5：错误恢复（shadow / active）
            # 检查 transport 层和业务层的错误
            _result_success = result.get("success", False)
            if _result_success:
                _data = result.get("data", {})
                if isinstance(_data, dict) and not _data.get("success", True):
                    _result_success = False
            if not _result_success and self._recovery_mode != "off":
                recovery = self._recover(
                    decision=decision,
                    result=result,
                    verification=verification,
                    after=after,
                )
                self._log_recovery(recovery, decision, result)

                if self._recovery_mode == "active" and recovery.kind != "none":
                    if recovery.kind == "reobserve":
                        # 强制重新观察，不重放旧动作。回到 LLM 重新决策
                        _log(f"  [recover] 强制重新观察，回到 LLM 重新决策")
                        continue

                    if recovery.kind == "retry" and recovery.retry_action:
                        _log(f"  [recover] 重试: {recovery.reason}")
                        # 重新执行相同动作（仅 NAVIGATION_TIMEOUT 且 URL 未变化时）
                        retry_result = await self._execute(decision.to_action_dict())
                        retry_success = retry_result.get("success", False)
                        if retry_success:
                            _log(f"  [recover] 重试成功")
                            # 把重试结果写入 history
                            self.history[-1] = {
                                "step": step,
                                "action": decision.to_action_dict(),
                                "success": True,
                                "error": "",
                            }
                            result = retry_result
                        else:
                            _log(f"  [recover] 重试仍失败，进入旧逻辑")
                        # 无论重试是否成功，继续后续流程（不进 continue）

            # 13. 循环防护：重复 no_effect 动作检测（shadow / active）
            if self._loop_guard_mode != "off":
                loop_recovery = self._handle_loop_guard(
                    decision=decision,
                    verification=verification,
                    after=after,
                )
                if loop_recovery is not None and self._loop_guard_mode == "active":
                    if loop_recovery.kind == "reobserve":
                        _log(f"  [loop_guard] 强制重新观察（{loop_recovery.reason}）")
                        continue

            # 14. 防死循环：同一元素失败 3 次则强制跳过（旧逻辑 fallback）
            target_id = decision.target_id
            if not result.get("success") and target_id:
                self._retry_counts[target_id] = self._retry_counts.get(target_id, 0) + 1
                if self._retry_counts[target_id] >= 3:
                    self.attempted.add(target_id)
                    _log(f"  元素 {target_id} 已失败 3 次，加入黑名单")

            # 14. 等待页面稳定
            await asyncio.sleep(0.5)

        return AgentResult(
            success=False,
            reason=f"达到最大步数 {self.max_steps}",
            steps=self.max_steps,
            history=self.history,
        )

    def _wrap_result(self, action: dict, result: dict) -> ActionResult:
        """把 _execute() 的 dict 结果包装为 ActionResult（统一错误分类）。"""
        action_type = action.get("action", "")
        success = result.get("success", False)
        if success:
            return ActionResult(action_type=action_type, transport_ok=True, raw=result)
        error_msg = str(result.get("error", ""))
        return ActionResult(
            action_type=action_type,
            transport_ok=False,
            error_kind=classify_error(None, {"error": error_msg}),
            error_message=error_msg,
            raw=result,
        )

    def _log_verification(self, verification: ActionVerification, effects: ActionEffects) -> None:
        """记录验证结果。结构化 + 文本双输出（shadow mode 只记录，不干预决策）。"""
        # 结构化事件
        self._emit_event("action_verification", {
            "status": verification.status,
            "transport_ok": verification.transport_ok,
            "page_responded": verification.page_responded,
            "expected_effect_seen": verification.expected_effect_seen,
            "error_kind": verification.error_kind.value if verification.error_kind else None,
            "effects": effects.to_dict(),
        })
        # 文本日志
        _log(
            f"  [verify] status={verification.status} "
            f"transport={verification.transport_ok} "
            f"responded={verification.page_responded} "
            f"expected={verification.expected_effect_seen} "
            f"effects={effects.to_dict()}"
        )

    # ── P8-C：GoalAssessment ─────────────────────────────────────────────

    def _assess_goal(
        self,
        action_type: str,
        before: PageSnapshot | None,
        after: PageSnapshot | None,
        effects: ActionEffects | None,
    ) -> GoalAssessment:
        """评估当前目标是否完成。基于显式规则，不依赖 LLM。"""
        current_goal = self.state.current_goal if self.state else None
        return assess_from_rules(
            action_type=action_type,
            before=before,
            after=after,
            effects=effects,
            current_goal=current_goal,
        )

    # ── P8-D：可观测性 ──────────────────────────────────────────────────

    def _emit_event(self, event_type: str, data: dict) -> None:
        """发出结构化事件。根据 AGENT_OBSERVABILITY 决定输出格式。

        事件包含关联 ID（session_id, step, action_id, timestamp），不含敏感字段。
        支持 stderr（文本）和 jsonl（结构化 JSON）两种格式。
        """
        if self._observability == "off":
            return

        self._event_count += 1
        import time as _time
        event = {
            "event": event_type,
            "seq": self._event_count,
            "session_id": self.state.session_id if self.state and hasattr(self.state, 'session_id') else "",
            "step": self.state.step if self.state else 0,
            "timestamp": _time.time(),
        }
        event.update(data)

        if self._observability == "jsonl":
            line = json.dumps(event, ensure_ascii=False)
            if self._observability_path:
                try:
                    with open(self._observability_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    _log(f"[event] write error: {self._observability_path}")
            else:
                # jsonl 到 stderr
                _log(f"[event] {line}")
        else:
            # stderr 文本格式
            parts = [f"[{event_type}]"]
            for k, v in data.items():
                if isinstance(v, float):
                    parts.append(f"{k}={v:.2f}")
                elif isinstance(v, bool):
                    parts.append(f"{k}={v}")
                elif isinstance(v, (int, str)):
                    parts.append(f"{k}={v}")
                elif isinstance(v, list):
                    parts.append(f"{k}_count={len(v)}")
                elif v is None:
                    parts.append(f"{k}=none")
            _log(" ".join(parts))

    def _log_assessment(self, assessment: GoalAssessment, applied: bool = False,
                    action_type: str = "", duration_ms: int = 0) -> None:
        """记录评估结果。结构化 + 文本双输出。

        applied: 评估是否被应用到状态更新（shadow 模式始终 False）。
        rejected_reason: 如果评估未应用，说明原因。
        """
        # 计算拒绝原因（shadow 模式始终未应用）
        rejected_reason = None
        apply_reason = None
        if not applied:
            if assessment.status != "completed":
                rejected_reason = f"status_{assessment.status}"
            elif assessment.source == "llm":
                rejected_reason = "llm_only_without_rule_evidence"
            elif assessment.confidence < 0.7:
                rejected_reason = f"low_confidence_{assessment.confidence:.2f}"
            elif assessment.required_confirmation:
                rejected_reason = "requires_confirmation"
            else:
                rejected_reason = "shadow_mode"
        else:
            apply_reason = f"{assessment.source}_{assessment.evidence[0].kind if assessment.evidence else 'unknown'}"

        # 结构化事件
        self._emit_event("goal_assessment", {
            "goal": str(assessment.goal or ""),
            "action_type": action_type,
            "status": assessment.status,
            "confidence": round(assessment.confidence, 2),
            "source": assessment.source,
            "stable": assessment.stable,
            "required_confirmation": assessment.required_confirmation,
            "evidence_kinds": [e.kind for e in assessment.evidence],
            "evidence_count": len(assessment.evidence),
            "applied": applied,
            "apply_reason": apply_reason,
            "rejected_reason": rejected_reason,
            "duration_ms": duration_ms,
        })
        # 文本日志（兼容）
        _log(
            f"  [goal] goal={assessment.goal or ''} "
            f"status={assessment.status} "
            f"confidence={assessment.confidence:.2f} "
            f"source={assessment.source} "
            f"evidence_count={len(assessment.evidence)} "
            f"applied={applied}"
        )

    # ── 阶段 5：错误恢复 ─────────────────────────────────────────────────

    def _recover(
        self,
        decision: Decision,
        result: dict,
        verification: ActionVerification | None,
        after: PageSnapshot | None,
    ) -> RecoveryDecision:
        """根据错误类型生成恢复决策。

        只处理三类错误：
          STALE_TARGET → reobserve（不重放旧动作）
          ELEMENT_NOT_FOUND → reobserve（不重放旧动作）
          NAVIGATION_TIMEOUT → check URL / retry once
        其他错误 → none（保留旧逻辑 fallback）

        result 来自 _execute()，其 success 表示 transport 层是否成功。
        某些动作（如 navigate）即使 transport 成功也可能返回业务错误，
        需要检查 result 中的 data 字段。
        """
        action_type = decision.action_type

        # 检查 transport 层和业务层的错误
        if not result.get("success", False):
            # transport 层失败
            error_msg = str(result.get("error", ""))
            error_kind = classify_error(None, {"error": error_msg})
        else:
            # transport 成功，检查 data 中是否有业务错误
            data = result.get("data", {})
            if isinstance(data, dict) and not data.get("success", True):
                error_msg = str(data.get("error", ""))
                error_kind = classify_error(None, {"error": error_msg})
            else:
                return RecoveryDecision(kind="none", reason="动作成功，无需恢复")

        # STALE_TARGET → 强制重新观察，不重放旧动作
        if error_kind == ErrorKind.STALE_TARGET:
            return RecoveryDecision(
                kind="reobserve",
                reason=f"stale target {decision.target_id}",
                force_reobserve=True,
            )

        # ELEMENT_NOT_FOUND → 强制重新观察
        if error_kind == ErrorKind.ELEMENT_NOT_FOUND:
            return RecoveryDecision(
                kind="reobserve",
                reason=f"element {decision.target_id} not found",
                force_reobserve=True,
            )

        # NAVIGATION_TIMEOUT → 检查 URL 是否已到达目标
        if error_kind == ErrorKind.NAVIGATION_TIMEOUT:
            # 导航超时：只有 after 的 URL 已到达目标 URL 时才视为可能成功。
            # 注意：after_url 可能是上一个导航的 URL（如 a.com），
            # 不等于当前目标（b.com），此时应重试而非误判为"已变化"。
            target_url = decision.url or ""
            after_url = after.url if after and after.snapshot_ok else ""
            url_reached = bool(target_url) and bool(after_url) and after_url == target_url
            if url_reached:
                # URL 已变化，视为可能成功，继续
                return RecoveryDecision(
                    kind="none",
                    reason="navigation timeout but URL changed, proceeding",
                )
            # URL 未变化：检查重试次数
            action_id = f"navigate:{decision.url}"
            retries = self._recovery_retry_counts.get(action_id, 0)
            if retries < 1:
                self._recovery_retry_counts[action_id] = retries + 1
                return RecoveryDecision(
                    kind="retry",
                    reason="navigation timeout and URL unchanged, retry once",
                    retry_action=True,
                    max_attempts=1,
                )
            return RecoveryDecision(
                kind="none",
                reason="navigation timeout and retry exhausted, fallback to legacy",
            )

        # 其他错误 → 保留旧逻辑
        return RecoveryDecision(kind="none", reason=f"unhandled error: {error_kind.value}")

    def _log_recovery(self, recovery: RecoveryDecision, decision: Decision, result: dict) -> None:
        """记录恢复决策。结构化 + 文本双输出。不输出敏感信息。"""
        action_type = decision.action_type
        target_id = decision.target_id
        data = result.get("data", {})
        if isinstance(data, dict) and not data.get("success", True):
            error = str(data.get("error", ""))[:60]
        else:
            error = str(result.get("error", ""))[:60]

        # 结构化事件
        self._emit_event("recovery_decision", {
            "mode": self._recovery_mode,
            "kind": recovery.kind,
            "action_type": action_type,
            "target_id": target_id,
            "error_preview": error,
            "reason": recovery.reason,
            "retry_action": recovery.retry_action,
            "force_reobserve": recovery.force_reobserve,
        })
        # 文本日志
        _log(
            f"  [recover] mode={self._recovery_mode} "
            f"kind={recovery.kind} "
            f"action={action_type} "
            f"target={target_id} "
            f"error={error} "
            f"reason={recovery.reason}"
        )

    # ── 循环防护（重复 no_effect 动作检测） ──────────────────────────────

    def _no_effect_signature(self, decision: Decision, page_fingerprint: str | None) -> str:
        """生成动作签名，用于检测重复 no_effect 动作。

        使用有限长度的短摘要（action_type + target_id + URL + fingerprint digest），
        避免把完整 DOM 指纹放入 key / 日志。
        """
        import hashlib
        digest = ""
        if page_fingerprint:
            digest = hashlib.sha256(page_fingerprint.encode("utf-8")).hexdigest()[:12]
        return f"{decision.action_type}:{decision.target_id}:{digest}"

    def _handle_loop_guard(self, decision: Decision, verification: ActionVerification | None,
                           after: PageSnapshot | None) -> RecoveryDecision | None:
        """检测重复 no_effect 动作。如果同一签名连续多次 no_effect，触发恢复。

        策略：
        - 第 1 次 no_effect → 正常继续
        - 第 2 次相同 no_effect → reobserve
        - 第 3 次相同 no_effect → 暂时屏蔽 target
        - 页面变化 / 动作变化 → 重置计数
        """
        if self._loop_guard_mode == "off":
            return None

        status = verification.status if verification else "unknown"
        action_type = decision.action_type
        target_id = decision.target_id

        if status != "no_effect":
            # 有有效效果，重置计数
            self._no_effect_counts.clear()
            self._last_action_signature = ""
            return None

        # 生成签名
        fingerprint = after.dom_fingerprint if after else None
        sig = self._no_effect_signature(decision, fingerprint)

        # 如果签名变化，重置计数
        if sig != self._last_action_signature and self._last_action_signature:
            self._no_effect_counts.clear()

        count = self._no_effect_counts.get(sig, 0) + 1
        self._no_effect_counts[sig] = count
        self._last_action_signature = sig

        if count == 1:
            # 第 1 次 no_effect，正常继续
            return None

        if count == 2:
            # 第 2 次相同 no_effect → reobserve
            self._emit_event("loop_guard_decision", {
                "kind": "no_effect_repeat",
                "action_type": action_type,
                "target_id": target_id,
                "repeat_count": count,
                "next_strategy": "reobserve",
                "executed": self._loop_guard_mode == "active",
            })
            if self._loop_guard_mode == "active":
                return RecoveryDecision(
                    kind="reobserve",
                    reason=f"no_effect repeat {count}x on {action_type}:{target_id}",
                    force_reobserve=True,
                )
            return None

        if count >= 3:
            # 第 3 次相同 no_effect → 暂时屏蔽 target
            self._emit_event("loop_guard_decision", {
                "kind": "no_effect_repeat",
                "action_type": action_type,
                "target_id": target_id,
                "repeat_count": count,
                "next_strategy": "block_target",
                "executed": self._loop_guard_mode == "active",
            })
            if self._loop_guard_mode == "active":
                self.attempted.add(target_id)
                # C3-3：记录 blocked_target 供 LLM 上下文反馈
                if self.state and target_id:
                    self.state.blocked_targets[target_id] = f"repeated_no_effect ({count}x) on {action_type}"
                    if len(self.state.blocked_targets) > 20:
                        self.state.blocked_targets = dict(list(self.state.blocked_targets.items())[-20:])
                _log(f"  [loop_guard] 屏蔽 target {target_id}（{count}x no_effect）")
                return RecoveryDecision(
                    kind="reobserve",
                    reason=f"blocked target {target_id} after {count}x no_effect",
                    force_reobserve=True,
                )
            return None

        return None

    # ── C3-1：动作前 target 验证 ────────────────────────────────────────

    async def _fetch_element_info(self, target_id: str) -> dict | None:
        """读取目标元素的 DOM 信息（tag/role/visible/enabled/connected）。

        用于 execute 前验证。页面正在导航时返回 None（unknown）。
        """
        expr = r"""
        (() => {
          const el = document.querySelector('[data-agent-id="%s"]');
          if (!el) return null;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return {
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role') || '',
            type: el.getAttribute('type') || '',
            visible: rect.width > 0 && rect.height > 0 &&
                     style.display !== 'none' &&
                     style.visibility !== 'hidden',
            enabled: !el.disabled,
            connected: el.isConnected,
            contenteditable: el.isContentEditable || false,
          };
        })()
        """ % target_id.replace('"', '\\"')
        try:
            val = await self._evaluate_parse(expr)
            return val if isinstance(val, dict) else None
        except Exception:
            return None

    async def _validate_action_target(self, decision: Decision) -> TargetValidation | None:
        """执行前验证目标元素。只有 action_guard_mode 非 off 时执行。

        返回 None 表示无需验证（如 evaluate/stop/pause 等不需要 target 的动作）。
        """
        if self._action_guard_mode == "off":
            return None

        action_type = decision.action_type
        target_id = decision.target_id

        # 不需要 target 的动作
        if action_type in ("stop", "pause", "evaluate", "download_setup", "navigate"):
            return None

        if not target_id:
            return TargetValidation("invalid", "", action_type, reason="target_id 为空")

        # 读取元素信息
        info = await self._fetch_element_info(target_id)
        result = validate_target(target_id, action_type, info)

        # 日志
        if self._action_guard_mode != "off":
            _log(
                f"  [action_guard] mode={self._action_guard_mode} "
                f"action={action_type} target={target_id} "
                f"status={result.status} reason={result.reason or ''}"
            )

        return result

    # ── 阶段 6A：Checkpoint / Pause / Resume ──────────────────────────────

    async def _create_checkpoint(
        self,
        observation: tuple[str, dict],
        decision: Decision,
        pause_reason: str = "waiting_for_user",
    ) -> Checkpoint:
        """创建当前状态的 checkpoint。snapshot 失败时创建有限 checkpoint。"""
        tree, meta = observation
        page_url = str(meta.get("url", ""))
        snapshot_available = True

        try:
            snapshot = await self._snapshot()
            fingerprint = snapshot.dom_fingerprint
            if not snapshot.snapshot_ok:
                snapshot_available = False
        except Exception:
            fingerprint = None
            snapshot_available = False

        import uuid
        ck = Checkpoint(
            version=1,
            checkpoint_id=str(uuid.uuid4())[:8],
            session_id=str(uuid.uuid4())[:8],
            task=self.state.task if self.state else "",
            current_goal=self.state.current_goal if self.state else None,
            next_goal=self.state.next_goal if self.state else None,
            completed_goals=list(self.state.completed_goals) if self.state else [],
            failed_attempts=list(self.state.failed_attempts) if self.state else [],
            step=self.state.step if self.state else 0,
            page_url=page_url,
            page_fingerprint=fingerprint,
            pause_reason=pause_reason,
            snapshot_available=snapshot_available,
        )
        # P8-D：checkpoint 生命周期事件
        self._emit_event("checkpoint_created", {
            "checkpoint_id": ck.checkpoint_id,
            "pause_reason": pause_reason,
            "page_url": page_url,
            "snapshot_available": snapshot_available,
        })
        _log(f"  [checkpoint] created id={ck.checkpoint_id} reason={pause_reason} url={page_url}")
        return ck

    async def _match_checkpoint(
        self, checkpoint: Checkpoint, observation: tuple[str, dict]
    ) -> PageMatchLevel:
        """判断当前页面与 checkpoint 的匹配程度。

        不使用精确 DOM 指纹匹配（广告/动态内容会导致误报）。
        不使用旧 target_id 或旧 action 作为恢复依据。
        """
        _, meta = observation
        current_url = str(meta.get("url", ""))

        # 强匹配：URL 相同
        if current_url and checkpoint.page_url and current_url == checkpoint.page_url:
            return PageMatchLevel.STRONG

        # 弱匹配：URL 属于同一流程（同域名/同路径前缀）
        if current_url and checkpoint.page_url:
            from urllib.parse import urlparse
            try:
                ck_parsed = urlparse(checkpoint.page_url)
                cur_parsed = urlparse(current_url)
                if ck_parsed.netloc and ck_parsed.netloc == cur_parsed.netloc:
                    # 同域名视为弱匹配
                    return PageMatchLevel.WEAK
            except Exception:
                pass

        # 不匹配
        return PageMatchLevel.NONE

    async def resume(
        self,
        checkpoint: Checkpoint,
        new_task: str | None = None,
    ) -> "AgentResult":
        """从 checkpoint 恢复执行。恢复后重新观察页面，不使用旧 target/action。"""
        _log(f"\n=== 恢复任务: checkpoint={checkpoint.checkpoint_id} ===")

        # 初始化状态（从 checkpoint 恢复）
        initial_state = AgentState(task=new_task or checkpoint.task)
        initial_state.current_goal = checkpoint.current_goal
        initial_state.next_goal = checkpoint.next_goal
        initial_state.completed_goals = list(checkpoint.completed_goals)
        initial_state.failed_attempts = list(checkpoint.failed_attempts)
        initial_state.step = checkpoint.step

        # 重新观察页面
        observation = await self._observe()
        tree, meta = observation
        initial_state.observe(tree, meta)

        # 判断页面匹配
        match = await self._match_checkpoint(checkpoint, observation)
        # P8-D：resume 事件
        self._emit_event("resume_attempted", {
            "checkpoint_id": checkpoint.checkpoint_id,
            "page_match": match.value,
            "page_url": meta.get('url', ''),
        })
        _log(f"  [resume] 页面匹配: {match.value} (url={meta.get('url', '')})")

        if match == PageMatchLevel.NONE:
            _log(f"  [resume] 页面不匹配，重新规划")
            initial_state.current_goal = None
            initial_state.goal_status = "not_started"

        elif match == PageMatchLevel.WEAK:
            _log(f"  [resume] 页面弱匹配，继续当前目标但可能需重新规划")

        # 进入主循环，传入 initial_state 避免 run() 重新初始化
        return await self.run(new_task or checkpoint.task, initial_state=initial_state)

    async def _observe(self) -> tuple[str, dict]:
        """获取当前页面状态。

        CDP 大页面响应可能触发 chunk 解析错误（"Separator is not found"）。
        此时 tree 返回错误文本，meta 仍可正常获取。
        """
        try:
            meta = await self.browser.meta()
        except Exception as e:
            meta = {"url": "", "title": "", "interactiveCount": 0}

        try:
            tree = await self.browser.tree()
        except Exception as e:
            error_msg = str(e)
            # 检测 CDP 大页面解析错误
            if "chunk" in error_msg.lower() and ("separator" in error_msg.lower() or "exceed" in error_msg.lower()):
                tree = f"[PAGE_TOO_LARGE] CDP 响应解析失败，页面可能过大。建议使用 evaluate 获取特定元素。{error_msg[:100]}"
            else:
                tree = f"[获取页面树失败: {error_msg}]"

        return tree, meta

    # ── 阶段 2：浏览器快照适配器 ─────────────────────────────────────────
    # 把真实 browser 状态转成 PageSnapshot。字段允许为 None（底层不支持时）。

    async def _snapshot(self) -> PageSnapshot:
        """采样当前页面状态。快照失败时返回 snapshot_ok=False，不抛异常。"""
        try:
            meta = await self.browser.meta()
        except Exception as e:
            return PageSnapshot(
                target_id="",
                url="",
                title="",
                dom_fingerprint=None,
                form_state={},
                targets=(),
                snapshot_ok=False,
            )

        target_id = str(meta.get("target_id", "") or meta.get("id", "") or "")
        url = str(meta.get("url", ""))
        title = str(meta.get("title", ""))

        # 指纹/表单/目标信息各自 best-effort，失败不影响主快照
        fingerprint = await self._dom_fingerprint()
        form_state = await self._form_state()
        focused = await self._focused_element()
        targets = await self._target_infos()

        return PageSnapshot(
            target_id=target_id,
            url=url,
            title=title,
            dom_fingerprint=fingerprint,
            form_state=form_state,
            focused_element=focused,
            targets=targets,
            snapshot_ok=True,
        )

    async def _evaluate_parse(self, expression: str) -> Any:
        """执行 evaluate 并解析返回值为 Python 对象。

        browser.send_command('evaluate') 返回的 result 字段是 JSON 字符串，
        需要统一用 json.loads 解析，而不是直接当作 dict 使用。
        """
        try:
            resp = await self.browser.send_command("evaluate", expression=expression)
            raw = resp.get("result", "")
            if isinstance(raw, str):
                import json as _json
                return _json.loads(raw)
            return raw
        except Exception:
            return None

    async def _dom_fingerprint(self) -> str | None:
        """生成可交互元素归一化指纹（role/aria-label/name/type/href/截断文本）。

        仅用于比较，不把完整结构放入 LLM 上下文。失败返回 None。
        """
        expr = r"""
        (() => {
          const MAX_NODES = 500;
          const MAX_TEXT = 120;
          const els = Array.from(document.querySelectorAll(
            'button, a, input, textarea, select, [role=button], [role=link], ' +
            '[role=textbox], [role=checkbox], [role=radio], [role=tab], [role=menuitem]'
          ));
          const parts = [];
          for (const el of els.slice(0, MAX_NODES)) {
            const role = el.getAttribute('role') || el.tagName.toLowerCase();
            const label = (el.getAttribute('aria-label') || el.getAttribute('name') ||
                           el.getAttribute('placeholder') || '').slice(0, MAX_TEXT);
            const type = el.getAttribute('type') || '';
            const href = el.getAttribute('href') || '';
            const text = (el.textContent || '').trim().slice(0, MAX_TEXT);
            parts.push([role, type, label, href, text].join('|'));
          }
          return parts.join('\n');
        })()
        """
        try:
            val = await self._evaluate_parse(expr)
            if isinstance(val, str):
                return val if val else None
            if isinstance(val, (list, dict)):
                return json.dumps(val, ensure_ascii=False)
            return str(val) if val else None
        except Exception:
            return None

    async def _form_state(self) -> dict:
        """采集表单输入状态。密码字段脱敏，只记录 changed/length。

        input.value 读 property（不是 HTML attribute）。
        """
        expr = r"""
        (() => {
          const out = {};
          const els = document.querySelectorAll('input, textarea, select');
          for (const el of els) {
            const name = el.getAttribute('name') || el.id || '';
            if (!name) continue;
            const type = (el.getAttribute('type') || 'text').toLowerCase();
            if (type === 'password') {
              const v = el.value || '';
              out[name] = { kind: 'password', changed: v.length > 0, length: v.length };
            } else if (type === 'checkbox' || type === 'radio') {
              out[name] = { kind: type, checked: !!el.checked };
            } else if (el.tagName === 'SELECT') {
              out[name] = { kind: 'select', value: el.value || '' };
            } else if (type === 'file') {
              out[name] = { kind: 'file', has_file: (el.files && el.files.length > 0) };
            } else {
              out[name] = { kind: type || 'text', value: el.value || '' };
            }
          }
          return out;
        })()
        """
        try:
            val = await self._evaluate_parse(expr)
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}

    async def _focused_element(self) -> dict | None:
        """采集当前页面焦点元素信息。只记录元素身份，不记录 value。"""
        expr = r"""
        (() => {
          const el = document.activeElement;
          if (!el || el === document.body) return null;
          return {
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            name: el.getAttribute('name') || '',
            role: el.getAttribute('role') || '',
            type: el.getAttribute('type') || '',
          };
        })()
        """
        try:
            val = await self._evaluate_parse(expr)
            return val if isinstance(val, dict) and val.get("tag") else None
        except Exception:
            return None

    async def _target_infos(self) -> tuple[TargetInfo, ...]:
        """采集当前 agent 相关 target 列表。底层不支持时返回空元组。"""
        try:
            resp = await self.browser.send_command("targets")
        except Exception:
            return ()
        try:
            raw = resp.get("targets") or resp.get("result") or []
            if not isinstance(raw, list):
                return ()
            infos = []
            for t in raw:
                if not isinstance(t, dict):
                    continue
                tid = str(t.get("id") or t.get("targetId") or "")
                if not tid:
                    continue
                ttype = str(t.get("type") or t.get("targetType") or "page")
                infos.append(
                    TargetInfo(
                        target_id=tid,
                        target_type=ttype,
                        url=str(t.get("url") or ""),
                        opener_id=str(t.get("openerId") or "") or None,
                        window_id=t.get("windowId") or None,
                    )
                )
            return tuple(infos)
        except Exception:
            return ()

    async def _snapshot_after_action(
        self, action_type: str, settle_ms: int | None = None
    ) -> PageSnapshot:
        """动作后采样。等待页面稳定后再采样，避免导航/动画未完成导致误报。"""
        settle = settle_ms if settle_ms is not None else ACTION_SETTLE_TIMEOUT.get(action_type, 500)
        await asyncio.sleep(settle / 1000.0)
        return await self._snapshot()

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
                # C3-3：优先使用结构化 set_value（对 textarea/combobox 更可靠）
                try:
                    eval_req = EvaluateRequest(
                        operation="set_value",
                        target_id=target_id,
                        value=text,
                    )
                    script = generate_script(eval_req)
                    if script:
                        resp = await self.browser.send_command("evaluate", expression=script)
                        return {"success": True, "data": resp}
                except Exception:
                    pass
                # fallback: 原生 type_text
                resp = await self.browser.type_text(target_id, text)
                return {"success": True, "data": resp}

            elif act == "evaluate":
                # 尝试结构化操作（C3-2）
                eval_req = normalize_evaluate_operation(action)
                if eval_req and eval_req.operation:
                    script = generate_script(eval_req)
                    if script:
                        resp = await self.browser.send_command("evaluate", expression=script)
                        return {"success": True, "data": resp}
                    return {"success": False, "error": f"evaluate 脚本生成失败: {eval_req.operation}"}

                # raw evaluate（LLM 直接生成 JS）
                expression = action.get("expression", "")
                if not expression:
                    return {"success": False, "error": "缺少 expression 参数"}
                if self._raw_evaluate_mode == "off":
                    return {"success": False, "error": "raw evaluate 已禁用（AGENT_RAW_EVALUATE=off）"}
                if self._raw_evaluate_mode == "shadow":
                    _log(f"  [raw_evaluate] 长度={len(expression)} 前100字={expression[:100]}")
                resp = await self.browser.send_command("evaluate", expression=expression)
                return {"success": True, "data": resp}

            # 结构化操作（C3-2：直接作为 action 输出）
            elif act in STRUCTURED_EVALUATE_ACTIONS:
                eval_req = EvaluateRequest(
                    operation=act,
                    target_id=action.get("target_id", ""),
                    value=action.get("value", ""),
                    property_name=action.get("property_name", ""),
                )
                script = generate_script(eval_req)
                if not script:
                    return {"success": False, "error": f"操作 {act} 脚本生成失败"}
                resp = await self.browser.send_command("evaluate", expression=script)
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