# -*- coding: utf-8 -*-
"""按消耗量触发审查 / 压缩随消耗收紧（v0.2.1.7 框架侧）的离线测试。"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.context import ResultCompressor  # noqa: E402
from core.daemon import Reviewer  # noqa: E402


class FakeDaemon:
    def __init__(self, every=32, budget=0):
        self.cfg = {}
        self.review_every = every
        self.review_token_budget = budget
        self.review_llm = False

    def log(self, *a, **k):
        pass

    def query_health(self):
        return None


def agent(total, sub=0):
    return SimpleNamespace(total_tokens=total, sub_tokens=sub, recent_results=[])


class ReviewTokenTriggerTest(unittest.TestCase):
    def test_consumption_triggers_early_review(self):
        """轮数间隔(32)远未到，但消耗超预算 → 应触发审查。"""
        r = Reviewer(FakeDaemon(every=32, budget=1000))
        r.reset()
        ag = agent(1500)
        r.check(1, ag)
        self.assertEqual(r.last_review_round, 1)
        self.assertEqual(r.last_review_tokens, 1500)

    def test_under_budget_no_trigger(self):
        r = Reviewer(FakeDaemon(every=32, budget=1000))
        ag = agent(500)
        r.check(1, ag)
        self.assertEqual(r.last_review_round, 0)

    def test_budget_zero_keeps_round_only(self):
        """预算 0（关闭）→ 维持旧行为：只按轮数。"""
        r = Reviewer(FakeDaemon(every=5, budget=0))
        ag = agent(999_999)
        r.check(1, ag)
        self.assertEqual(r.last_review_round, 0)
        r.check(5, ag)
        self.assertEqual(r.last_review_round, 5)

    def test_counter_resets_after_review(self):
        r = Reviewer(FakeDaemon(every=32, budget=1000))
        ag = agent(1500)
        r.check(1, ag)
        ag.total_tokens = 1600          # 只多了 100，不足预算
        r.check(2, ag)
        self.assertEqual(r.last_review_round, 1)   # 未再触发
        ag.total_tokens = 2600          # 累计又超 1000
        r.check(3, ag)
        self.assertEqual(r.last_review_round, 3)

    def test_reset_clears_token_counter(self):
        r = Reviewer(FakeDaemon(every=32, budget=1000))
        ag = agent(1500)
        r.check(1, ag)
        r.reset()
        self.assertEqual(r.last_review_tokens, 0)
        self.assertEqual(r.last_review_round, 0)


class CompressorEscalationTest(unittest.TestCase):
    def test_bands(self):
        c = ResultCompressor(350)
        c.set_usage(0)
        self.assertEqual(c.threshold, 350)
        c.set_usage(100_000)
        self.assertEqual(c.threshold, 233)
        c.set_usage(200_000)
        self.assertEqual(c.threshold, 175)
        c.set_usage(300_000)
        self.assertEqual(c.threshold, 87)

    def test_small_results_never_compressed(self):
        c = ResultCompressor(350)
        c.set_usage(300_000)
        self.assertEqual(c.compress("t", "短结果"), "短结果")

    def test_escalation_compresses_more(self):
        c = ResultCompressor(350)
        self.assertEqual(c.compress("t", "x" * 100), "x" * 100)   # 原档：100<350 不压
        c.set_usage(300_000)                                       # 阈值收到 87
        out = c.compress("t", "x" * 100)
        self.assertIn("已存档", out)

    def test_spool_fetch_still_works(self):
        c = ResultCompressor(350)
        c.set_usage(300_000)
        out = c.compress("t", "y" * 200)
        self.assertIn("r1", out)
        self.assertEqual(c.fetch("r1")["length"], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
