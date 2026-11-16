import unittest

from trading_paper.simulation.optimizer import _double_execution_costs, _promotion_eligible


class OptimizerCostGateTest(unittest.TestCase):
    def test_stress_scenario_doubles_costs_without_mutating_base(self):
        base = {'execution': {'fee_bps_per_side': 5, 'slippage_bps': 5,
                              'fallback_spread_bps_total': 10}}
        stressed = _double_execution_costs(base)
        self.assertEqual(base['execution'], {'fee_bps_per_side': 5, 'slippage_bps': 5,
                                             'fallback_spread_bps_total': 10})
        self.assertEqual(stressed['execution'], {'fee_bps_per_side': 10, 'slippage_bps': 10,
                                                 'fallback_spread_bps_total': 20})

    def test_small_holdout_trade_count_cannot_promote_a_candidate(self):
        baseline = {'closed_trades': 10, 'net_return_pct': 0.0, 'max_drawdown_pct': 1.0}
        candidate = {'closed_trades': 19, 'net_return_pct': 1.0, 'max_drawdown_pct': 1.0}
        self.assertFalse(_promotion_eligible(
            has_selected_settings=True, selected_train_score=1.0, baseline_train_score=0.0,
            validation={**candidate, 'closed_trades': 10}, baseline_validation=baseline,
            holdout=candidate, baseline_holdout=baseline, stressed_holdout=candidate,
            baseline_stressed_holdout=baseline, minimum_validation_trades=10,
            minimum_holdout_trades=20, max_drawdown_pct=5.0,
        ))

    def test_candidate_passes_only_when_all_chronological_and_stress_gates_pass(self):
        baseline = {'closed_trades': 20, 'net_return_pct': 0.0, 'max_drawdown_pct': 1.0}
        candidate = {'closed_trades': 20, 'net_return_pct': 0.5, 'max_drawdown_pct': 2.0}
        self.assertTrue(_promotion_eligible(
            has_selected_settings=True, selected_train_score=1.0, baseline_train_score=0.0,
            validation={**candidate, 'closed_trades': 10}, baseline_validation=baseline,
            holdout=candidate, baseline_holdout=baseline, stressed_holdout=candidate,
            baseline_stressed_holdout=baseline, minimum_validation_trades=10,
            minimum_holdout_trades=20, max_drawdown_pct=5.0,
        ))


if __name__ == '__main__':
    unittest.main()
