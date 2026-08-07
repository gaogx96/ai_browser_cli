"""诊断 focus_changed 问题：验证 CDP click 是否触发原生 focus。

运行：
    python tests/diagnose_focus.py
    # 需要 Chrome 已打开（扩展模式或 9222 端口）
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdk", "python"))


async def main():
    from browser_client import BrowserClient

    browser = BrowserClient()
    await browser.start()
    try:
        # 1. 导航到百度
        print("导航到百度...")
        resp = await browser.navigate("https://www.baidu.com")
        print(f"  标题: {resp.get('title', 'N/A')}")
        await asyncio.sleep(2)

        # 2. 获取页面树
        tree = await browser.tree()
        print(f"页面树 (前 20 行):")
        for line in tree.split("\n")[:20]:
            print(f"  {line}")

        # 3. 诊断 A: 检查 activeElement 初始状态
        expr_a = r"""
        (() => {
          const el = document.activeElement;
          return {
            tag: el ? el.tagName : 'null',
            id: el ? el.id : '',
            name: el ? (el.getAttribute('name') || '') : '',
            isBody: el === document.body,
            hasInput: !!document.querySelector('input'),
          };
        })()
        """
        resp = await browser.send_command("evaluate", expression=expr_a)
        result = resp.get("result", {})
        print(f"\n诊断 A: activeElement 初始状态")
        print(f"  {json.dumps(result, ensure_ascii=False)}")

        # 4. 诊断 B: 找到第一个输入框
        expr_b = r"""
        (() => {
          const inputs = document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button])');
          const first = inputs[0];
          if (!first) return {found: false, count: inputs.length};
          return {
            found: true,
            tag: first.tagName,
            id: first.id,
            name: first.getAttribute('name'),
            type: first.getAttribute('type'),
            rect: first.getBoundingClientRect(),
          };
        })()
        """
        resp = await browser.send_command("evaluate", expression=expr_b)
        result = json.loads(resp.get("result", "{}"))
        print(f"\n诊断 B: 找到输入框")
        print(f"  {json.dumps(result, ensure_ascii=False, default=str)}")

        # 5. 诊断 C: 通过 evaluate 点击输入框，然后检查 activeElement
        input_name = result.get("name", "wd")
        expr_c = r"""
        (() => {
          const input = document.querySelector('input[name="%s"], input[type="text"]');
          if (!input) return {clicked: false, reason: 'input not found'};
          input.click();
          return {
            clicked: true,
            beforeTag: document.activeElement ? document.activeElement.tagName : 'null',
            afterTag: input.tagName,
            afterId: input.id,
            afterName: input.getAttribute('name'),
            activeIsInput: document.activeElement === input,
            inputValue: input.value.substring(0, 20),
          };
        })()
        """ % input_name
        resp = await browser.send_command("evaluate", expression=expr_c)
        result = json.loads(resp.get("result", "{}"))
        print(f"\n诊断 C: evaluate click + 检查 activeElement")
        print(f"  {json.dumps(result, ensure_ascii=False)}")

        # 6. 诊断 D: 通过 element.focus() 直接聚焦
        expr_d = r"""
        (() => {
          const input = document.querySelector('input[name="%s"], input[type="text"]');
          if (!input) return {focused: false, reason: 'input not found'};
          input.focus();
          return {
            focused: true,
            activeTag: document.activeElement ? document.activeElement.tagName : 'null',
            activeId: document.activeElement ? document.activeElement.id : '',
            activeName: document.activeElement ? (document.activeElement.getAttribute('name') || '') : '',
            isInput: document.activeElement === input,
          };
        })()
        """ % input_name
        resp = await browser.send_command("evaluate", expression=expr_d)
        result = json.loads(resp.get("result", "{}"))
        print(f"\n诊断 D: element.focus() 后 activeElement")
        print(f"  {json.dumps(result, ensure_ascii=False)}")

        # 7. 诊断 E: 检查页面上所有可见输入框
        expr_e = r"""
        (() => {
          const inputs = Array.from(document.querySelectorAll('input, textarea'));
          return inputs.map((el, i) => {
            const r = el.getBoundingClientRect();
            return {
              idx: i,
              tag: el.tagName,
              id: el.id,
              name: el.getAttribute('name'),
              type: el.getAttribute('type') || '',
              visible: r.width > 0 && r.height > 0,
              rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
            };
          });
        })()
        """
        resp = await browser.send_command("evaluate", expression=expr_e)
        result = json.loads(resp.get("result", "[]"))
        print(f"\n诊断 E: 所有输入/文本域")
        print(f"  {json.dumps(result, ensure_ascii=False, default=str)}")

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())