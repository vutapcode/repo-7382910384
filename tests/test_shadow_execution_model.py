import unittest

from loi_he_thong import shadow_execution_model as model


class ShadowExecutionModelTests(unittest.TestCase):
    def test_market_fill_is_adverse_after_crossing_spread(self):
        self.assertGreater(model.market_fill('LONG', 99.9, 100.0, 2.0), 100.0)
        self.assertLess(model.market_fill('SHORT', 99.9, 100.0, 2.0), 99.9)

    def test_maker_requires_post_placement_aggressor_trade_through(self):
        trades = [
            {'thoi_gian_ms': 900, 'gia': 99.9, 'khoi_luong': 1, 'ban_chu_dong': True},
            {'thoi_gian_ms': 1_050, 'gia': 100.0, 'khoi_luong': 2, 'ban_chu_dong': False},
            {'thoi_gian_ms': 1_100, 'gia': 99.9, 'khoi_luong': 0.004, 'ban_chu_dong': True},
        ]
        self.assertEqual(
            model.maker_trade_through_volume('LONG', 99.9, 1.0, trades, now=1.2),
            0.004,
        )
        self.assertEqual(
            model.maker_trade_through_volume('SHORT', 100.0, 1.0, trades, now=1.2),
            2.0,
        )

    def test_default_queue_proxy_is_five_times_order_quantity(self):
        self.assertEqual(model.maker_fill_required_volume(0.001), 0.005)

    def test_disordered_trade_buffer_uses_correct_full_fallback(self):
        trades = [
            {'thoi_gian_ms': 1_100, 'gia': 99.9, 'khoi_luong': 0.003,
             'ban_chu_dong': True},
            {'thoi_gian_ms': 1_050, 'gia': 99.9, 'khoi_luong': 0.002,
             'ban_chu_dong': True},
        ]
        self.assertEqual(
            model.maker_trade_through_volume(
                'LONG', 99.9, 1.0, trades, now=1.2
            ),
            0.005,
        )


if __name__ == '__main__':
    unittest.main()
