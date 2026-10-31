import math
import unittest

from forecast import make_forecast, parse_history


class ForecastTests(unittest.TestCase):
    def test_next_day_uses_previous_week_and_explicit_scale(self):
        text = 'date,energy_kwh\n' + ''.join(f'2025-01-{i:02d},{i}\n' for i in range(1, 15))
        result = make_forecast(text, .01)
        self.assertEqual(result['predicted_kwh'], 8)
        self.assertEqual(result['target_date'], '2025-01-15')
        self.assertEqual(result['cutoff_date'], '2025-01-14')
        self.assertAlmostEqual(result['budget_wh'], .08)
        self.assertEqual(result['mae_kwh'], 7)
        self.assertEqual(result['source'], 'uploaded')

    def test_bad_dates_values_and_headers_fail(self):
        good = 'date,energy_kwh\n' + ''.join(f'2025-01-{i:02d},10\n' for i in range(1, 9))
        for text in (good.replace('2025-01-02', '2025-01-01'),
                     good.replace('energy_kwh', 'power_kw'), good.replace(',10', ',nan', 1),
                     good.replace(',10', ',-1', 1), 'date,energy_kwh\n2025-01-01,1\n'):
            with self.subTest(text=text[:60]), self.assertRaises(ValueError):
                parse_history(text)

    def test_reproducible_demo_and_invalid_scale(self):
        self.assertEqual(make_forecast(), make_forecast())
        self.assertEqual(make_forecast()['source'], 'synthetic')
        for scale in (0, -1, math.nan, math.inf, True, None):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                make_forecast(scale_wh_per_kwh=scale)

    def test_weekday_mean_with_only_available_history(self):
        text = 'date,energy_kwh\n' + ''.join(f'2025-01-{i:02d},{i}\n' for i in range(1, 29))
        result = make_forecast(text, model='weekday_mean')
        self.assertEqual(result['predicted_kwh'], (22 + 15 + 8 + 1) / 4)
        self.assertEqual(result['backtest_n'], 21)


if __name__ == '__main__':
    unittest.main()
