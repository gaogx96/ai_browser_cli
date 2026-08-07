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
    new_targets: list[TargetInfo] = field(default_factory=list)
    closed_targets: list[TargetInfo] = field(default_factory=list)

    def any_change(self) -> bool:
        return any(
            [
                self.url_changed,
                self.title_changed,
                self.dom_changed,
                self.form_changed,
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
    "click": {"url_changed", "dom_changed", "new_targets"},
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


def classify_error(exc: Exception | None, raw_result: dict | None = None) -> ErrorKind:
    """把异常/原始结果统一归类为 ErrorKind。所有 _execute() 错误都经过此入口。"""
    if exc is not None:
        msg = str(exc).lower()
        if "not found" in msg or "not exist" in msg or "not in any frame" in msg:
            return ErrorKind.ELEMENT_NOT_FOUND
        if "stale" in msg or "detached" in msg or "already disposed" in msg:
            return ErrorKind.STALE_TARGET
        if "timeout" in msg or "timed out" in msg:
            return ErrorKind.NAVIGATION_TIMEOUT
        if "not interactable" in msg or "not clickable" in msg or "covered" in msg:
            return ErrorKind.ELEMENT_NOT_INTERACTABLE
        if "permission" in msg or "login" in msg or "captcha" in msg or "风控" in msg:
            return ErrorKind.PERMISSION_REQUIRED
        return ErrorKind.UNKNOWN

    if raw_result:
        err = str(raw_result.get("error", "")).lower()
        if "not found" in err or "not exist" in err:
            return ErrorKind.ELEMENT_NOT_FOUND
        if "stale" in err or "detached" in err:
            return ErrorKind.STALE_TARGET
        if "timeout" in err:
            return ErrorKind.NAVIGATION_TIMEOUT
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
        return d


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
    # 扁平格式: {"action": "click", "target_id": "e5"}
    else:
        action_type = str(action)
        target_id = raw.get("target_id", "")
        url = raw.get("url", "")
        text = raw.get("text", "")
        expression = raw.get("expression", "")
        path = raw.get("path", "")
        reason = raw.get("reason", "")

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
    goal_status: str = "not_started"  # not_started | in_progress | completed | failed
    completed_goals: list[str] = field(default_factory=list)
    failed_attempts: list[str] = field(default_factory=list)
    last_observation_summary: str = ""
    step: int = 0

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
        if decision.next_goal:
            self.next_goal = decision.next_goal
        if self.current_goal is None and self.next_goal:
            self.current_goal = self.next_goal
            self.goal_status = "in_progress"

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
        lines.append("最近观察:")
        lines.append(self.last_observation_summary)
        return "\n".join(lines)


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

    async def run(self, task: str) -> AgentResult:
        """执行一个自然语言浏览器任务。"""
        self.history = []
        self.attempted = set()
        self._retry_counts = {}
        self._last_snapshot = None
        self.state = AgentState(task=task)

        for step in range(1, self.max_steps + 1):
            _log(f"\n--- Step {step}/{self.max_steps} ---")

            # 1. 观察
            tree, meta = await self._observe()

            # 2. 阶段 4：更新 AgentState 观察
            self.state.observe(tree, meta)

            # 3. 决策
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

            # 7. 阶段 3：shadow 验证前置快照（仅记录，不干预）
            if self._verify_mode != "off":
                before = await self._snapshot()
            else:
                before = None

            # 8. 执行
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

            # 12. 防死循环：同一元素失败 3 次则强制跳过
            target_id = decision.target_id
            if not result.get("success") and target_id:
                self._retry_counts[target_id] = self._retry_counts.get(target_id, 0) + 1
                if self._retry_counts[target_id] >= 3:
                    self.attempted.add(target_id)
                    _log(f"  元素 {target_id} 已失败 3 次，加入黑名单")

            # 13. 等待页面稳定
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
        """记录验证结果到 stderr（shadow mode 只记录，不干预决策）。"""
        _log(
            f"  [verify] status={verification.status} "
            f"transport={verification.transport_ok} "
            f"responded={verification.page_responded} "
            f"expected={verification.expected_effect_seen} "
            f"effects={effects.to_dict()}"
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
        targets = await self._target_infos()

        return PageSnapshot(
            target_id=target_id,
            url=url,
            title=title,
            dom_fingerprint=fingerprint,
            form_state=form_state,
            targets=targets,
            snapshot_ok=True,
        )

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
            resp = await self.browser.send_command("evaluate", expression=expr)
        except Exception:
            return None
        try:
            val = resp.get("result", "")
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
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
            resp = await self.browser.send_command("evaluate", expression=expr)
        except Exception:
            return {}
        try:
            val = resp.get("result", {})
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}

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