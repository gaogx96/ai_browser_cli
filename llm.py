"""
LLM 封装层 — 支持 Anthropic 和 OpenAI 两种后端。

为 Agent 提供决策能力：给定任务描述、页面树、历史，返回下一步动作 JSON。
"""

import json
import os
import re
from typing import Any

# ── System Prompt ──────────────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = r"""你是一个通过无障碍语义树（Accessibility Tree）操作真实浏览器的自动化 AI Agent。
你的任务是阅读当前页面的语义骨架，结合用户的目标，自主推理并输出下一步的精准浏览器控制指令。

## 网页无障碍树输入规范

你看到的页面由底层 Rust 浏览器提取为精炼的无障碍树。格式严格遵循：

[@eX] role "text_content"
- eX：该交互元素的全局唯一 ID（例如 e0, e12）。跨 frame 时带 [frame-1] 前缀。
- role：HTML 标签或语义角色（button, link, input, textbox, group, text 等）。
- "text_content"：该元素的文本内容、placeholder 或 aria-label。

## 你可以使用的动作

你必须且只能选择以下动作之一。输出为单行 JSON，不可带 Markdown 包裹块。

1. navigate：打开 URL。
   参数：{"action":"navigate","url":"https://..."}

2. click：点击元素。
   参数：{"action":"click","target_id":"eX"}

3. type：在输入框中输入文本。
   参数：{"action":"type","target_id":"eX","text":"输入的文本"}

4. evaluate：执行任意 JavaScript。当你需要绕过点击拦截、获取完整 URL、或直接操作 DOM 时使用。
   参数：{"action":"evaluate","expression":"JS 代码"}

5. download_setup：设置下载目录。下载文件前必须先调用此动作。
   参数：{"action":"download_setup","path":"C:/path/to/desktop"}

6. stop：任务完成或遇到无法逾越的障碍时终止循环。
   参数：{"action":"stop","reason":"成功完成任务/失败原因"}

7. pause：需要用户介入时暂停（如验证码、登录墙、文件选择）。
   参数：{"action":"pause","reason":"需要用户处理验证码"}

## 输出格式约束

必须且只能输出一个合法的单行 JSON 字典，不带 Markdown 包裹块。

### 基础格式（旧版兼容）
输出格式示例：
{"thought":"当前在登录页。需要先输入账号。","action":"type","target_id":"e0","text":"my_account"}

### 结构化格式（推荐，当【Agent 状态】块存在时使用）
当输入中包含【Agent 状态】块时，请使用以下格式输出：
{"thought":"当前观察和推理","evaluation_previous_goal":"上一步目标的完成情况：已完成/部分完成/失败/不适用","next_goal":"下一步要完成的具体目标","action":"click","target_id":"e5"}

## 目标管理指导

当输入中包含【Agent 状态】块时：
- 评估上一步目标是否完成（evaluation_previous_goal）
- 基于当前页面状态和任务进度，提出下一步目标（next_goal）
- 避免重复已经完成的目标
- 如果上一步目标部分完成，在 next_goal 中继续完成它

## 错误恢复指导

- 如果 click 返回错误，说明元素不可点击或已变化。尝试重新获取页面树，找替代元素。
- 如果 navigate 后页面树为空，等 1-2 秒再重试。
- 如果同一元素连续失败 3 次，放弃该策略，换其他方式。
- 对于下载场景：先 download_setup，再点击下载按钮。如果下载按钮被 JS 拦截，改用 evaluate 触发下载。
- 遇到验证码/登录墙/风控时，使用 pause 让用户处理。

## 实战范例

范例 A（登录）：
页面树：[@e0] input "手机号" [@e1] input "密码" [@e2] button "登录"
输出：{"thought":"需要先输入账号","action":"type","target_id":"e0","text":"user@test.com"}

范例 B（获取完整链接）：
当页面树中链接被截断（末尾有...），用 evaluate 获取完整 href：
{"thought":"链接被截断，用 evaluate 获取完整 URL","action":"evaluate","expression":"document.querySelector('a[href*=\"alidocs\"]').href"}

范例 C（结构化格式 + 目标管理）：
{"thought":"登录页，需要先输入账号","evaluation_previous_goal":"不适用，第一步","next_goal":"输入账号","action":"type","target_id":"e0","text":"user@test.com"}

范例 D（完成）：
{"thought":"任务已完成","action":"stop","reason":"需求文档已下载到桌面"}
"""


# ── LLM 客户端 ─────────────────────────────────────────────────────────────

class LLMClient:
    """LLM 客户端封装。支持 Anthropic 和 OpenAI 两种后端。"""

    def __init__(self, provider: str = "anthropic"):
        self.provider = provider
        self._client = None

        if provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 环境变量未设置")
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            model = os.environ.get("ANTHROPIC_MODEL")
            if not model:
                raise RuntimeError("ANTHROPIC_MODEL 环境变量未设置（例如 deepseek-v4-flash）")
            self._client = _AnthropicClient(key=key, base_url=base_url)
            self._model = model

        elif provider == "openai":
            import openai
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY 环境变量未设置")
            model = os.environ.get("OPENAI_MODEL")
            if not model:
                raise RuntimeError("OPENAI_MODEL 环境变量未设置（例如 gpt-4o）")
            self._client = openai.AsyncOpenAI(api_key=key)
            self._model = model

        elif provider == "deepseek":
            # DeepSeek 使用 OpenAI-compatible 格式，但 httpx 直连（避免 SDK 依赖）
            key = os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                raise RuntimeError("DEEPSEEK_API_KEY 环境变量未设置")
            base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            model = os.environ.get("DEEPSEEK_MODEL")
            if not model:
                raise RuntimeError("DEEPSEEK_MODEL 环境变量未设置（例如 deepseek-v4-flash）")
            self._client = _OpenAICompatibleClient(key=key, base_url=base_url)
            self._model = model

        else:
            raise ValueError(f"不支持的 LLM provider: {provider}")

    async def decide(
        self,
        task: str,
        tree: str,
        meta: dict,
        history: list,
        extra_context: str | None = None,
    ) -> dict[str, Any]:
        """给定任务、页面树、元数据、历史，返回下一步动作 JSON。

        extra_context: 可选的 AgentState 结构化上下文。提供时优先于原始 history，
        用于滚动式目标分解（current_goal / next_goal / completed_goals）。
        """

        history_text = "\n".join(
            f"Step {h['step']}: action={json.dumps(h['action'], ensure_ascii=False)} "
            f"result={'成功' if h.get('success') else '失败: ' + str(h.get('error', ''))}"
            for h in history[-5:]  # 只给最近 5 步，避免上下文过长
        )

        page_info = f"当前 URL: {meta.get('url', '')}\n标题: {meta.get('title', '')}\n交互元素数: {meta.get('interactiveCount', 0)}"

        if extra_context:
            # 结构化上下文模式（阶段 4）：用 AgentState 提供滚动式目标分解
            user_prompt = f"""用户任务：{task}

{page_info}

当前页面树：
{tree}

【Agent 状态】
{extra_context}

请输出下一步动作（单行 JSON，不要 Markdown 包裹块）。输出格式：
{{"thought":"...","evaluation_previous_goal":"上一步目标评估","next_goal":"下一步要完成的目标","action":"click","target_id":"e5"}}"""
        else:
            # 旧模式：直接用 history 列表
            user_prompt = f"""用户任务：{task}

{page_info}

当前页面树：
{tree}

历史操作（最近 5 步）：
{history_text if history else "无"}

请输出下一步动作（单行 JSON，不要 Markdown 包裹块）："""

        messages = [
            {"role": "user", "content": user_prompt},
        ]

        if self.provider == "anthropic":
            raw = await self._client.messages_create(
                model=self._model,
                max_tokens=512,
                system=AGENT_SYSTEM_PROMPT,
                messages=messages,
            )

        elif self.provider == "openai":
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                    *messages,
                ],
            )
            raw = resp.choices[0].message.content or ""

        elif self.provider == "deepseek":
            raw = await self._client.chat_completions_create(
                model=self._model,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                    *messages,
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                reasoning_effort="low",
            )

        else:
            raise ValueError(f"不支持的 provider: {self.provider}")

        return self._parse_action(raw)

    def _parse_action(self, raw: str) -> dict[str, Any]:
        """从 LLM 输出中解析 JSON 动作。"""
        # 去除可能的 Markdown 包裹块
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw)

        # 尝试解析 JSON
        try:
            action = json.loads(raw)
        except json.JSONDecodeError:
            # 尝试在文本中找第一个 { ... }
            match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw)
            if match:
                try:
                    action = json.loads(match.group())
                except json.JSONDecodeError:
                    action = {"action": "stop", "reason": f"LLM 输出解析失败: {raw[:200]}"}
            else:
                action = {"action": "stop", "reason": f"LLM 输出无合法 JSON: {raw[:200]}"}

        # 校验必填字段
        if "action" not in action:
            action = {"action": "stop", "reason": f"LLM 输出缺少 action 字段: {raw[:200]}"}

        # 校验 action 值合法性（含结构化操作：focus/set_value/scroll_into_view 等）
        valid_actions = {
            "navigate", "click", "type", "evaluate", "download_setup", "stop", "pause",
            "focus", "set_value", "scroll_into_view", "dispatch_input", "dispatch_change", "read_property",
        }
        if action["action"] not in valid_actions:
            action = {"action": "stop", "reason": f"LLM 输出了非法 action: {action.get('action')}"}

        # 校验参数完整性
        if action["action"] == "navigate" and "url" not in action:
            action["action"] = "stop"
            action["reason"] = "navigate 缺少 url 参数"
        if action["action"] == "click" and "target_id" not in action:
            action["action"] = "stop"
            action["reason"] = "click 缺少 target_id 参数"
        if action["action"] == "type" and ("target_id" not in action or "text" not in action):
            action["action"] = "stop"
            action["reason"] = "type 缺少 target_id 或 text 参数"
        if action["action"] == "evaluate" and "expression" not in action:
            action["action"] = "stop"
            action["reason"] = "evaluate 缺少 expression 参数"

        return action


# ── Anthropic 直连客户端（不依赖 SDK，避免 auth header 兼容问题） ──────────


class _AnthropicClient:
    """使用 httpx 直接调用 Anthropic API（兼容 sensenova 等中转服务）。"""

    def __init__(self, key: str, base_url: str):
        import httpx
        self._key = key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            timeout=60,
        )

    async def messages_create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
    ) -> str:
        """调用 Anthropic messages API，返回文本内容。"""
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        resp = await self._client.post("/v1/messages", json=body)
        if resp.status_code != 200:
            error_text = resp.text[:500]
            raise RuntimeError(f"Anthropic API 调用失败 (HTTP {resp.status_code}): {error_text}")
        data = resp.json()
        # 解析 content 数组，提取文本
        texts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)


# ── OpenAI-compatible 客户端（DeepSeek 等） ──────────────────────────────


class _OpenAICompatibleClient:
    """使用 httpx 直连 OpenAI-compatible API（DeepSeek 等）。

    请求格式：POST /v1/chat/completions
    响应解析：data["choices"][0]["message"]["content"]
    """

    def __init__(self, key: str, base_url: str):
        import httpx
        self._key = key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=120,
        )

    async def chat_completions_create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list[dict],
        response_format: dict | None = None,
        temperature: float = 0.1,
        reasoning_effort: str = "low",
        stream: bool = False,
    ) -> str:
        """调用 OpenAI-compatible chat completions API，返回文本内容。"""
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        if response_format:
            body["response_format"] = response_format
        # reasoning_effort 是 DeepSeek 扩展参数，非标准 OpenAI 字段
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort

        resp = await self._client.post("/v1/chat/completions", json=body)
        if resp.status_code != 200:
            error_text = resp.text[:500]
            raise RuntimeError(
                f"OpenAI-compatible API 调用失败 (HTTP {resp.status_code}): {error_text}"
            )
        data = resp.json()
        # 解析 choices[0].message.content
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"无法解析 OpenAI-compatible 响应: {str(data)[:300]}")