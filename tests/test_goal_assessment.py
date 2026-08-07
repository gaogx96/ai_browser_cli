"""P8-C 单元测试：GoalAssessment 数据模型、规则评估、shadow 模式。

运行：
    python tests/test_goal_assessment.py
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import (
    ActionEffects,
    GoalAssessment,
    GoalEvidence,
    PageSnapshot,
    TargetInfo,
    assess_from_rules,
    merge_rule_and_llm_assessment,
    should_accept_completion,
)


class TestGoalEvidence(unittest.TestCase):
    def test_evidence_fields(self):
        e = GoalEvidence("url_match", "URL 到达目标", weight=0.9)
        self.assertEqual(e.kind, "url_match")
        self.assertEqual(e.weight, 0.9)


class TestGoalAssessment(unittest.TestCase):
    def test_default_unknown(self):
        a = GoalAssessment()
        self.assertEqual(a.status, "unknown")
        self.assertEqual(a.confidence, 0.0)

    def test_to_dict(self):
        a = GoalAssessment(
            goal="打开百度", status="completed", confidence=0.9,
            evidence=[GoalEvidence("url_match", "URL 已变化")],
            source="rule", stable=True,
        )
        d = a.to_dict()
        self.assertEqual(d["goal"], "打开百度")
        self.assertEqual(d["status"], "completed")
        self.assertEqual(d["confidence"], 0.9)
        self.assertEqual(d["evidence_count"], 1)
        self.assertEqual(d["source"], "rule")
        self.assertTrue(d["stable"])


class TestAssessFromRules(unittest.TestCase):
    def setUp(self):
        self.t1 = TargetInfo("t1", "page")
        self.snap_before = PageSnapshot("t1", "about:blank", "初始页", "fp1", {}, (self.t1,), True)
        self.snap_after = PageSnapshot("t1", "https://example.com", "Example", "fp2", {}, (self.t1,), True)

    def test_navigate_url_changed_completed(self):
        effects = ActionEffects(url_changed=True, title_changed=True)
        a = assess_from_rules("navigate", self.snap_before, self.snap_after, effects, "打开 example.com")
        self.assertEqual(a.status, "completed")
        self.assertGreaterEqual(a.confidence, 0.7)
        self.assertEqual(a.source, "rule")
        self.assertTrue(a.stable)

    def test_navigate_no_change_unknown(self):
        effects = ActionEffects()
        a = assess_from_rules("navigate", self.snap_before, self.snap_before, effects, "打开 example.com")
        self.assertEqual(a.status, "unknown")

    def test_click_new_target_completed(self):
        t2 = TargetInfo("t2", "page")
        after = PageSnapshot("t1", "https://a.com", "A", "fp", {}, (self.t1, t2), True)
        effects = ActionEffects(new_targets=[t2])
        a = assess_from_rules("click", self.snap_before, after, effects, "点击打开链接")
        self.assertEqual(a.status, "completed")
        self.assertGreaterEqual(a.confidence, 0.8)

    def test_click_url_changed_completed(self):
        effects = ActionEffects(url_changed=True, dom_changed=True)
        a = assess_from_rules("click", self.snap_before, self.snap_after, effects, "点击登录")
        self.assertEqual(a.status, "completed")
        self.assertGreaterEqual(a.confidence, 0.7)

    def test_type_form_changed_completed(self):
        before = PageSnapshot("t1", "u", "t", "fp", {"q": {"kind": "text", "value": ""}}, (), True)
        after = PageSnapshot("t1", "u", "t", "fp", {"q": {"kind": "text", "value": "rust"}}, (), True)
        effects = ActionEffects(form_changed=True)
        a = assess_from_rules("type", before, after, effects, "输入搜索词")
        self.assertEqual(a.status, "completed")
        self.assertEqual(a.confidence, 0.7)

    def test_type_no_change_unknown(self):
        effects = ActionEffects()
        a = assess_from_rules("type", self.snap_before, self.snap_before, effects, "输入搜索词")
        self.assertEqual(a.status, "unknown")


class TestMergeAssessment(unittest.TestCase):
    def test_both_completed_high_confidence(self):
        rule = GoalAssessment(goal="测试", status="completed", confidence=0.8,
                              evidence=[GoalEvidence("url_match", "URL 变化")],
                              source="rule", stable=True)
        llm = GoalAssessment(goal="测试", status="completed", confidence=0.7,
                             evidence=[GoalEvidence("llm_judgment", "LLM 认为完成")],
                             source="llm")
        merged = merge_rule_and_llm_assessment(rule, llm)
        self.assertEqual(merged.status, "completed")
        self.assertEqual(merged.source, "combined")
        self.assertGreater(merged.confidence, 0.8)

    def test_rule_completed_llm_unknown_requires_confirmation(self):
        rule = GoalAssessment(goal="测试", status="completed", confidence=0.8,
                              evidence=[GoalEvidence("url_match", "URL 变化")],
                              source="rule", stable=True)
        llm = GoalAssessment(goal="测试", status="unknown", confidence=0.0)
        merged = merge_rule_and_llm_assessment(rule, llm)
        self.assertEqual(merged.status, "completed")
        self.assertTrue(merged.required_confirmation)

    def test_rule_unknown_llm_completed_partial(self):
        rule = GoalAssessment(goal="测试", status="unknown", confidence=0.0)
        llm = GoalAssessment(goal="测试", status="completed", confidence=0.6,
                             evidence=[GoalEvidence("llm_judgment", "LLM 认为完成")],
                             source="llm")
        merged = merge_rule_and_llm_assessment(rule, llm)
        self.assertEqual(merged.status, "partial")
        self.assertTrue(merged.required_confirmation)

    def test_no_llm_returns_rule(self):
        rule = GoalAssessment(goal="测试", status="completed", confidence=0.8,
                              evidence=[GoalEvidence("url_match", "URL 变化")],
                              source="rule", stable=True)
        merged = merge_rule_and_llm_assessment(rule, None)
        self.assertEqual(merged.status, "completed")
        self.assertEqual(merged.source, "rule")
        self.assertTrue(merged.required_confirmation)  # LLM 不确认，需要确认


class TestShouldAccept(unittest.TestCase):
    def test_rule_high_confidence_accepted(self):
        a = GoalAssessment(goal="测试", status="completed", confidence=0.8,
                           source="rule", stable=True)
        self.assertTrue(should_accept_completion(a))

    def test_rule_low_confidence_not_accepted(self):
        a = GoalAssessment(goal="测试", status="completed", confidence=0.5,
                           source="rule", stable=True)
        self.assertFalse(should_accept_completion(a))

    def test_combined_high_confidence_accepted(self):
        a = GoalAssessment(goal="测试", status="completed", confidence=0.9,
                           source="combined", stable=True)
        self.assertTrue(should_accept_completion(a))

    def test_combined_low_confidence_not_accepted(self):
        a = GoalAssessment(goal="测试", status="completed", confidence=0.8,
                           source="combined", stable=True)
        self.assertFalse(should_accept_completion(a))

    def test_llm_only_high_confidence_stable_accepted(self):
        a = GoalAssessment(goal="测试", status="completed", confidence=0.95,
                           source="llm", stable=True)
        self.assertTrue(should_accept_completion(a))

    def test_llm_only_not_stable_not_accepted(self):
        a = GoalAssessment(goal="测试", status="completed", confidence=0.95,
                           source="llm", stable=False)
        self.assertFalse(should_accept_completion(a))

    def test_unknown_not_accepted(self):
        a = GoalAssessment(goal="测试", status="unknown", confidence=0.0)
        self.assertFalse(should_accept_completion(a))

    def test_partial_not_accepted(self):
        a = GoalAssessment(goal="测试", status="partial", confidence=0.5)
        self.assertFalse(should_accept_completion(a))

    def test_failed_not_accepted(self):
        a = GoalAssessment(goal="测试", status="failed", confidence=0.0)
        self.assertFalse(should_accept_completion(a))


if __name__ == "__main__":
    unittest.main(verbosity=2)