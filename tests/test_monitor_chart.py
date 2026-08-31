"""Offline contract tests for the monitor page 30-day trend chart (UV-based).

These tests intentionally read the template only.  They do not import the
Flask app, start refresh threads, or need any upstream/LLM credentials.
"""

from pathlib import Path
import unittest


TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "monitor.html"


class MonitorChartUvContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")
        start = cls.source.index("function renderChart")
        end = cls.source.index("function renderRegions", start)
        cls.render_chart = cls.source[start:end]

    def test_bar_height_uses_uv(self):
        """柱高必须按 UV 归一化，不能再出现纯 PV 柱高。"""
        self.assertIn("const maxUv = Math.max(...daily.map(x => x.uv), 1)",
                      self.render_chart)
        self.assertIn("(d.uv / maxUv * 100)", self.render_chart)
        self.assertNotIn("maxPv", self.render_chart)
        self.assertNotIn("d.pv / maxPv * 100", self.render_chart)

    def test_peak_label_is_uv(self):
        """副标题峰值文案必须是 UV。"""
        self.assertIn("峰值 UV", self.render_chart)
        self.assertNotIn("峰值 PV", self.render_chart)

    def test_tip_shows_uv_before_pv(self):
        """tip 中 UV 在前，PV 保留在后供对照。"""
        tip_start = self.render_chart.index('class="tip">')
        tip_end = self.render_chart.index("</div>", tip_start)
        tip = self.render_chart[tip_start:tip_end]
        self.assertIn("UV", tip)
        self.assertIn("PV", tip)
        self.assertLess(tip.index("UV"), tip.index("PV"))

    def test_card_title_mentions_independent_ip(self):
        """趋势卡片标题需体现「独立 IP」= UV 口径。"""
        self.assertIn('<h2>📈 近 30 天趋势（独立 IP）', self.source)


if __name__ == "__main__":
    unittest.main()
