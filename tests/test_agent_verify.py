"""阶段 1-3 单元测试：数据模型、纯函数、shadow verification。

只测纯函数，不访问真实浏览器。运行：
    python tests/test_agent_verify.py
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    ActionEffects,
    ActionResult,
    ActionVerification,
    ErrorKind,
    PageSnapshot,
    TargetInfo,
    _diff,
    _expected_effect_seen,
    _verify,
    classify_error,
    normalize_decision,
)


def mk_snapshot(url="https://a.com", title="A", fp="fp1", form=None, targets=()):
    return PageSnapshot(
        target_id="t1",
        url=url,
        title=title,
        dom_fingerprint=fp,
        form_state=form or {},
        targets=targets,
        snapshot_ok=True,
    )


class TestDiff(unittest.TestCase):
    def test_no_change(self):
        e = _diff(mk_snapshot(), mk_snapshot())
        self.assertFalse(e.any_change())

    def test_url_change(self):
        e = _diff(mk_snapshot(url="https://a.com"), mk_snapshot(url="https://b.com"))
        self.assertTrue(e.url_changed)
        self.assertTrue(e.any_change())

    def test_title_change(self):
        e = _diff(mk_snapshot(title="A"), mk_snapshot(title="B"))
        self.assertTrue(e.title_changed)

    def test_dom_change(self):
        e = _diff(mk_snapshot(fp="fp1"), mk_snapshot(fp="fp2"))
        self.assertTrue(e.dom_changed)

    def test_form_change(self):
        e = _diff(
            mk_snapshot(form={"q": {"kind": "text", "value": ""}}),
            mk_snapshot(form={"q": {"kind": "text", "value": "rust"}}),
        )
        self.assertTrue(e.form_changed)

    def test_new_target(self):
        t1 = TargetInfo("t1", "page")
        t2 = TargetInfo("t2", "page")
        e = _diff(mk_snapshot(targets=(t1,)), mk_snapshot(targets=(t1, t2)))
        self.assertEqual([x.target_id for x in e.new_targets], ["t2"])

    def test_closed_target(self):
        t1 = TargetInfo("t1", "page")
        t2 = TargetInfo("t2", "page")
        e = _diff(mk_snapshot(targets=(t1, t2)), mk_snapshot(targets=(t1,)))
        self.assertEqual([x.target_id for x in e.closed_targets], ["t2"])

    def test_snapshot_failure_no_change(self):
        bad = PageSnapshot("t1", "", "", None, {}, (), snapshot_ok=False)
        e = _diff(mk_snapshot(), bad)
        self.assertFalse(e.any_change())


class TestExpectedEffect(unittest.TestCase):
    def test_click_url_change(self):
        e = ActionEffects(url_changed=True)
        self.assertTrue(_expected_effect_seen("click", e))

    def test_click_new_target(self):
        e = ActionEffects(new_targets=[TargetInfo("t2", "page")])
        self.assertTrue(_expected_effect_seen("click", e))

    def test_type_form_change(self):
        e = ActionEffects(form_changed=True)
        self.assertTrue(_expected_effect_seen("type", e))

    def test_type_no_form_change_false(self):
        e = ActionEffects()
        self.assertFalse(_expected_effect_seen("type", e))

    def test_evaluate_no_expected(self):
        e = ActionEffects(dom_changed=True)
        self.assertFalse(_expected_effect_seen("evaluate", e))


class TestVerify(unittest.TestCase):
    def test_failed_transport(self):
        r = ActionResult("click", transport_ok=False, error_kind=ErrorKind.ELEMENT_NOT_FOUND)
        v = _verify("click", r, ActionEffects())
        self.assertEqual(v.status, "failed")
        self.assertFalse(v.transport_ok)
        self.assertTrue(v.needs_reobserve)

    def test_success_expected(self):
        r = ActionResult("click", transport_ok=True)
        e = ActionEffects(url_changed=True)
        v = _verify("click", r, e)
        self.assertEqual(v.status, "success")

    def test_no_effect(self):
        r = ActionResult("click", transport_ok=True)
        v = _verify("click", r, ActionEffects())
        self.assertEqual(v.status, "no_effect")

    def test_unknown_for_non_visual(self):
        r = ActionResult("evaluate", transport_ok=True)
        v = _verify("evaluate", r, ActionEffects())
        self.assertEqual(v.status, "unknown")

    def test_none_result_success(self):
        e = ActionEffects(url_changed=True)
        v = _verify("navigate", None, e)
        self.assertEqual(v.status, "success")
        self.assertTrue(v.transport_ok)


class TestClassifyError(unittest.TestCase):
    def test_not_found(self):
        self.assertEqual(
            classify_error(Exception("Element [e5] not found in any frame")),
            ErrorKind.ELEMENT_NOT_FOUND,
        )

    def test_stale(self):
        self.assertEqual(classify_error(Exception("stale element")), ErrorKind.STALE_TARGET)

    def test_timeout(self):
        self.assertEqual(classify_error(Exception("timed out after 10s")), ErrorKind.NAVIGATION_TIMEOUT)

    def test_not_interactable(self):
        self.assertEqual(
            classify_error(Exception("element not interactable")),
            ErrorKind.ELEMENT_NOT_INTERACTABLE,
        )

    def test_permission(self):
        self.assertEqual(
            classify_error(Exception("captcha required")), ErrorKind.PERMISSION_REQUIRED
        )

    def test_unknown(self):
        self.assertEqual(classify_error(Exception("some weird error")), ErrorKind.UNKNOWN)

    def test_raw_dict(self):
        self.assertEqual(
            classify_error(None, {"error": "target not found"}),
            ErrorKind.ELEMENT_NOT_FOUND,
        )


class TestNormalizeDecision(unittest.TestCase):
    def test_old_format(self):
        d = normalize_decision({"action": "click", "target_id": "e5"})
        self.assertEqual(d.action_type, "click")
        self.assertEqual(d.target_id, "e5")
        self.assertFalse(d.is_pause)

    def test_new_flat_format(self):
        d = normalize_decision(
            {"next_goal": "找到结果", "action": "click", "target_id": "e5"}
        )
        self.assertEqual(d.action_type, "click")
        self.assertEqual(d.next_goal, "找到结果")

    def test_new_nested_format(self):
        d = normalize_decision(
            {"action": {"type": "type", "target_id": "e3", "text": "rust"}}
        )
        self.assertEqual(d.action_type, "type")
        self.assertEqual(d.target_id, "e3")
        self.assertEqual(d.text, "rust")

    def test_pause(self):
        d = normalize_decision({"action": "pause", "reason": "需要用户处理"})
        self.assertTrue(d.is_pause)
        self.assertEqual(d.reason, "需要用户处理")

    def test_to_action_dict(self):
        d = normalize_decision({"action": "click", "target_id": "e5"})
        self.assertEqual(d.to_action_dict(), {"action": "click", "target_id": "e5"})


if __name__ == "__main__":
    unittest.main(verbosity=2)