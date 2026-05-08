"""R-INTEL30 Phase 1 — unit tests for the 11 new intel modules.

Tests are pure-Python (no network calls). For each module we verify:
    1. fetch_all() exists and is async
    2. format_for_telegram(empty/error data) returns a string
    3. format_for_telegram(canonical-shape mock data) renders correctly
    4. Module degrades gracefully when API key is missing

Run with: pytest tests/test_intel30_phase1.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestModuleContracts(unittest.TestCase):
    """Each module must expose async fetch_all() + str format_for_telegram()."""

    MODULES = [
        "modules.intel30.hl_info_api",
        "modules.intel30.asxn_data",
        "modules.intel30.hypurrscan",
        "modules.intel30.fred_api",
        "modules.intel30.farside_etfs",
        "modules.intel30.arkham_intel",
        "modules.intel30.eia_oil",
        "modules.intel30.isw_ctp",
        "modules.intel30.criptoya_ar",
        "modules.intel30.bcra_macro",
        "modules.intel30.apollo_spark",
    ]

    def test_each_module_has_required_callables(self):
        for name in self.MODULES:
            mod = __import__(name, fromlist=["fetch_all", "format_for_telegram"])
            self.assertTrue(hasattr(mod, "fetch_all"), f"{name} missing fetch_all")
            self.assertTrue(hasattr(mod, "format_for_telegram"), f"{name} missing format_for_telegram")
            self.assertTrue(
                inspect.iscoroutinefunction(mod.fetch_all),
                f"{name}.fetch_all must be async",
            )

    def test_format_handles_empty_data(self):
        for name in self.MODULES:
            mod = __import__(name, fromlist=["format_for_telegram"])
            text = mod.format_for_telegram({})
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 0, f"{name} returned empty string for empty input")


class TestHlInfoApi(unittest.TestCase):
    def test_format_with_dexs(self):
        from modules.intel30 import hl_info_api
        data = {
            "perp_dexs": {"dexs": [{"name": "TradeXYZ", "fullName": "Trade XYZ Markets"}], "_error": None},
            "predicted_fundings": {"fundings": {"BTC": {"HlPerp": 0.001}}, "_error": None},
        }
        out = hl_info_api.format_for_telegram(data)
        self.assertIn("HIP-3", out)
        self.assertIn("TradeXYZ", out)
        self.assertIn("BTC", out)


class TestCriptoYa(unittest.TestCase):
    def test_format_with_canonical_fx(self):
        from modules.intel30 import criptoya_ar
        data = {
            "fx": {"fx": {"oficial": 1000.0, "blue": 1500.0, "ccl": 1450.0, "mayorista": 990.0}, "_error": None},
            "arb": {"exchanges": {"binance": {"ask": 1490, "bid": 1480}}, "_error": None},
        }
        out = criptoya_ar.format_for_telegram(data)
        self.assertIn("Brecha", out)
        self.assertIn("oficial", out.lower())
        self.assertIn("USDT", out)


class TestBcraMacro(unittest.TestCase):
    def test_format_with_variables(self):
        from modules.intel30 import bcra_macro
        data = {"variables": [
            {"id": 1, "name": "Reservas Intl. (USD M)", "fecha": "2026-05-07", "valor": 22500.0, "_error": None},
            {"id": 5, "name": "Tasa Política Monetaria (%)", "fecha": "2026-05-07", "valor": 35.0, "_error": None},
        ]}
        out = bcra_macro.format_for_telegram(data)
        self.assertIn("BCRA", out)
        self.assertIn("Reservas", out)
        self.assertIn("35.00", out)


class TestFredApi(unittest.TestCase):
    def test_format_no_key(self):
        from modules.intel30 import fred_api
        out = fred_api.format_for_telegram({"_global_error": "FRED_API_KEY not set"})
        self.assertIn("FRED_API_KEY", out)

    def test_format_with_series(self):
        from modules.intel30 import fred_api
        data = {"series": [
            {"id": "VIXCLS", "name": "VIX", "fecha": "2026-05-07", "valor": 18.5, "_error": None},
            {"id": "DGS10", "name": "10Y Treasury Yield (%)", "fecha": "2026-05-07", "valor": 4.42, "_error": None},
        ]}
        out = fred_api.format_for_telegram(data)
        self.assertIn("VIX", out)
        self.assertIn("18.500", out)
        self.assertIn("4.420", out)


class TestFarsideEtfs(unittest.TestCase):
    def test_format_with_flows(self):
        from modules.intel30 import farside_etfs
        data = {"flows": [
            {"asset": "BTC", "date": "07 May 2026", "flow_musd": 285.4, "_error": None},
            {"asset": "ETH", "date": "07 May 2026", "flow_musd": -42.1, "_error": None},
            {"asset": "SOL", "date": "07 May 2026", "flow_musd": 8.3, "_error": None},
        ]}
        out = farside_etfs.format_for_telegram(data)
        self.assertIn("BTC", out)
        self.assertIn("285", out)
        self.assertIn("-42", out)

    def test_parse_latest_row(self):
        from modules.intel30 import farside_etfs
        html = """<table><tr><th>Date</th><th>IBIT</th><th>Total</th></tr>
        <tr><td>07 May 2026</td><td>250.0</td><td>285.4</td></tr></table>"""
        out = farside_etfs._parse_latest_row(html)
        self.assertEqual(out["date"], "07 May 2026")
        self.assertEqual(out["total_flow_musd"], 285.4)


class TestIswCtp(unittest.TestCase):
    def test_parse_rss(self):
        from modules.intel30 import isw_ctp
        rss = """<rss><channel>
        <item><title>Iran Update May 7</title><link>https://x.com/y</link><pubDate>Wed, 07 May 2026 12:00:00 GMT</pubDate></item>
        <item><title>RU Offensive Campaign</title><link>https://x.com/z</link><pubDate>Wed, 07 May 2026 13:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items = isw_ctp._parse_rss(rss, max_items=2)
        self.assertEqual(len(items), 2)
        self.assertIn("Iran", items[0]["title"])

    def test_format(self):
        from modules.intel30 import isw_ctp
        data = {"feeds": [
            {"label": "ISW", "items": [{"title": "T1", "link": "x", "date": "Wed, 07 May 2026 12:00 GMT"}], "_error": None},
        ]}
        out = isw_ctp.format_for_telegram(data)
        self.assertIn("Geopol", out)
        self.assertIn("T1", out)


class TestApolloSpark(unittest.TestCase):
    def test_format_no_items(self):
        from modules.intel30 import apollo_spark
        out = apollo_spark.format_for_telegram({"_error": "404", "items": []})
        self.assertIn("Apollo", out)
        self.assertIn("404", out)


class TestEiaOil(unittest.TestCase):
    def test_format_no_key(self):
        from modules.intel30 import eia_oil
        out = eia_oil.format_for_telegram({"_global_error": "EIA_API_KEY not set"})
        self.assertIn("EIA_API_KEY", out)


class TestArkham(unittest.TestCase):
    def test_format_no_key(self):
        from modules.intel30 import arkham_intel
        out = arkham_intel.format_for_telegram({"_global_error": "ARKHAM_API_KEY not set"})
        self.assertIn("ARKHAM_API_KEY", out)


class TestAsxn(unittest.TestCase):
    def test_format_with_data(self):
        from modules.intel30 import asxn_data
        data = {"data": {"buyback_usd_total": 12_500_000.0, "burn_hype_total": 380000.0}, "_error": None}
        out = asxn_data.format_for_telegram(data)
        self.assertIn("HYPE", out)


class TestHypurrscan(unittest.TestCase):
    def test_format_with_auction(self):
        from modules.intel30 import hypurrscan
        data = {"auctions": {"data": {"currentAuction": {"name": "FOO", "currentPrice": 250}}, "_error": None}}
        out = hypurrscan.format_for_telegram(data)
        self.assertIn("FOO", out)


class TestBotWiringIntegrity(unittest.TestCase):
    """Verify command/handler registry is consistent for R-INTEL30 entries."""

    def test_intel30_commands_in_registry(self):
        from commands_registry import COMMANDS
        cmd_names = {c.command for c in COMMANDS}
        for needed in ["etfs", "macro", "argy", "isw", "eia", "asxn", "hypurr",
                       "arkham", "hl_info", "spark", "intel30"]:
            self.assertIn(needed, cmd_names, f"/{needed} missing from COMMANDS registry")


if __name__ == "__main__":
    unittest.main()
