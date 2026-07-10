import unittest

import us_market_radar as radar


class TickerMatchingTests(unittest.TestCase):
    def test_short_tickers_do_not_match_common_english(self):
        self.assertFalse(radar.stock_matches("THE COMPANY WILL BE PROFITABLE", {"ticker": "P", "name": "P"}))
        self.assertFalse(radar.stock_matches("RATES MAY BE LOWER", {"ticker": "BE", "name": "BE"}))
        self.assertFalse(radar.stock_matches("MUCH STRONGER DEMAND", {"ticker": "MU", "name": "MU"}))

    def test_short_tickers_require_qualified_symbol_or_company_name(self):
        self.assertTrue(radar.stock_matches("BLOOM ENERGY RAISES GUIDANCE", {"ticker": "BE", "name": "Bloom Energy"}))
        self.assertTrue(radar.stock_matches("NYSE: BE RALLIES AFTER EARNINGS", {"ticker": "BE", "name": "Bloom Energy"}))
        self.assertTrue(radar.stock_matches("$P REPORTS RESULTS", {"ticker": "P", "name": "Everpure"}))

    def test_regular_ticker_and_company_alias_match(self):
        self.assertTrue(radar.stock_matches("NVDA SHARES RISE", {"ticker": "NVDA", "name": "NVIDIA"}))
        self.assertTrue(radar.stock_matches("MICROSOFT ANNOUNCES NEW CLOUD PRODUCT", {"ticker": "MSFT", "name": "Microsoft"}))
        self.assertFalse(radar.stock_matches("PANWORKS IS NOT A STOCK", {"ticker": "PANW", "name": "Palo Alto Networks"}))


class SignalMatrixTests(unittest.TestCase):
    def test_priority_requires_heat_and_eligible_entry(self):
        code, _ = radar.judge_signal(72, 80, True, "觀察層")
        self.assertEqual(code, "priority")

    def test_quiet_candidate_preserves_early_signal(self):
        code, _ = radar.judge_signal(68, 20, True, "觀察層")
        self.assertEqual(code, "quiet")

    def test_risk_veto_wins_over_high_composite_score(self):
        code, _ = radar.judge_signal(82, 90, False, "排除-波段新高(中後段)")
        self.assertEqual(code, "hot_risk")

    def test_build_rows_keeps_entry_and_buzz_separate(self):
        screener = [{
            "ticker": "NVDA",
            "name": "NVIDIA",
            "sector": "半導體",
            "theme": "AI晶片",
            "compositeScore": 75,
            "tier": "觀察層",
            "status": "OK",
        }]
        item = {
            "layer": "buzz",
            "source": "Example",
            "published_at": radar.datetime.now(radar.timezone.utc),
            "sentiment_score": 1,
            "stocks": [{"ticker": "NVDA", "name": "NVIDIA"}],
            "topics": ["AI晶片"],
        }
        rows = radar.build_radar_rows(screener, [item], {"results": []})
        self.assertEqual(rows[0]["entry_score"], 75)
        self.assertEqual(rows[0]["buzz_score"], 100)
        self.assertEqual(rows[0]["signal_code"], "priority")

    def test_same_article_is_not_double_counted_across_layers(self):
        base = {
            "url": "https://example.com/one-story",
            "title": "NVIDIA story",
            "source": "Example",
            "published_at": radar.datetime.now(radar.timezone.utc),
            "sentiment_score": 0,
            "stocks": [{"ticker": "NVDA", "name": "NVIDIA"}],
            "topics": ["AI晶片"],
        }
        first = {**base, "layer": "buzz"}
        second = {**base, "layer": "research"}
        ranked = radar.rank_news_stocks([first, second])
        topics = radar.rank_topics([first, second])
        self.assertEqual(ranked[0]["mentions"], 1)
        self.assertEqual(topics[0]["mentions"], 1)


class OutputSafetyTests(unittest.TestCase):
    def test_only_http_urls_are_rendered(self):
        self.assertEqual(radar.safe_url("javascript:alert(1)"), "")
        self.assertEqual(radar.safe_url("https://example.com/story"), "https://example.com/story")


if __name__ == "__main__":
    unittest.main()
