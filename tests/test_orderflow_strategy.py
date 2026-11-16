import unittest

from trading_paper.simulation.orderflow_live import exit_reason, stop_loss_triggered
from trading_paper.simulation.orderflow_strategy import ChannelBreakoutConfirmation


class ChannelBreakoutConfirmationTest(unittest.TestCase):
    def test_requires_warmup_breakout_and_two_flow_confirmations(self):
        strategy = ChannelBreakoutConfirmation()
        for second in range(240):
            self.assertIsNone(strategy.observe_quote(second, 100.0))
        level = strategy.observe_quote(240, 100.1)
        self.assertEqual(level, 100.0)
        features = [0.1, 0.1, 0.25, 0.1, 0.3, 0.2, 0.4]
        self.assertIsNone(strategy.confirm(241, 100.1, features, 3.0, 3_000_000))
        self.assertEqual(strategy.confirm(242, 100.1, features, 3.0, 3_000_000), 100.0)
        self.assertIsNone(strategy.confirm(243, 100.1, features, 3.0, 3_000_000))

    def test_rejects_wide_spread_and_resets_confirmation(self):
        strategy = ChannelBreakoutConfirmation()
        for second in range(240):
            strategy.observe_quote(second, 100.0)
        strategy.observe_quote(240, 100.1)
        features = [0.1, 0.1, 0.25, 0.1, 0.3, 0.2, 0.4]
        self.assertIsNone(strategy.confirm(241, 100.1, features, 6.0, 3_000_000))
        self.assertIsNone(strategy.confirm(242, 100.1, features, 3.0, 3_000_000))

    def test_model_policy_matches_fixed_30_second_label(self):
        self.assertFalse(stop_loss_triggered("F2_rules_plus_model", 996, 1000))
        self.assertTrue(stop_loss_triggered("F1_rules", 996, 1000))
        self.assertIsNone(exit_reason("F2_rules_plus_model", 10, 3))
        self.assertEqual(exit_reason("F2_rules_plus_model", 30, 0), "MAX_HOLD_30S")
        self.assertEqual(exit_reason("F1_rules", 10, 2), "FLOW_REVERSAL")


if __name__ == "__main__":
    unittest.main()
