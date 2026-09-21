"""Offline order regressions; no signing keys or exchange/network writes."""
from contextlib import ExitStack
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from follow_service import config as cfg, database as db, trader, moss_poller, moss_ws


class OrderPrecisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.path = Path(temp.name) / "config_123456.json"
        self.path.write_text(json.dumps({
            "main_address": "0x" + "11" * 20,
            "db_path": str(Path(temp.name) / "trades.db"),
            "hyperliquid_auth": {"mode": "contract_agent"},
            "agent_protocol_report": {"enabled": False},
            "follow_ratio": 1, "slippage_percent": 1.5,
        }))
        self.stack.enter_context(patch.object(cfg, "_config_path", self.path))
        self.stack.enter_context(patch.object(trader, "_leverage_cache", {}))
        self.stack.enter_context(patch.dict(trader._expected_pos, {}, clear=True))
        self.stack.enter_context(patch.object(db, "_baseline_init_seen", set()))
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network request")))
        self.stack.enter_context(patch.object(trader.hyper_coins, "canonicalize_coin", side_effect=lambda coin, **kw: coin))
        self.stack.enter_context(patch.object(trader.hyper_coins, "canonicalize_positions", side_effect=lambda positions, **kw: positions))
        for module in (trader, moss_poller, moss_ws):
            self.stack.enter_context(patch.object(module, "_is_coin_tradeable", return_value=True))
        self.info = SimpleNamespace(coin_to_asset={"MANTA": 123, "BTC": 0, "xyz:SNDK": 110000, "@1": 10001},
                                    asset_to_sz_decimals={123: 1, 0: 5, 110000: 2, 10001: 1})
        self.exchange = Mock()
        self.exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"oid": 42, "avgPx": "0.06539", "totalRawFeeUsdc": "0.001"}}
        ]}}}
        db.init_db()

    def assert_valid_price(self, px, sz_decimals, max_decimals=6):
        value = Decimal(str(px))
        self.assertTrue(value.is_finite())
        self.assertGreater(value, 0)
        normalized = value.normalize()
        self.assertLessEqual(max(0, -normalized.as_tuple().exponent), max_decimals - sz_decimals)
        if value != value.to_integral_value():
            self.assertLessEqual(len(normalized.as_tuple().digits), 5)

    def sync(self, *, size=3000, our_positions=None):
        trader._do_sync_coin(
            exchange=self.exchange, info=self.info, coin="MANTA", agent_address="source-fixture",
            agent_acct_val=1000, our_acct_values={"": 100},
            agent_positions={"MANTA": {"size": size, "leverage": 1}} if size else {},
            our_positions=our_positions or {}, baselines={}, mids={"MANTA": "0.06539"}, source="copy",
        )

    def last_trade(self):
        with db.get_conn() as conn:
            return dict(conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 1").fetchone())

    def test_manta_buy_and_sell_limit_precision_and_slippage_bound(self):
        # MANTA metadata: szDecimals=1, so a perp price allows at most 5 decimal places.
        for size in (3000, -3000):
            with self.subTest(size=size):
                trader._expected_pos.clear()
                self.exchange.order.reset_mock()
                self.sync(size=size)
                self.exchange.order.assert_called_once()
                price = self.exchange.order.call_args.args[3]
                self.assert_valid_price(price, 1)
                raw_limit = 0.06539 * (1.015 if size > 0 else 0.985)
                if size > 0:
                    self.assertLessEqual(price, raw_limit)
                    self.assertEqual(price, 0.06637)
                else:
                    self.assertGreaterEqual(price, raw_limit)
                    self.assertEqual(price, 0.06441)
                self.assertEqual(self.last_trade()["order_price"], price)

    def test_exchange_rejection_reason_reaches_database(self):
        self.exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
            {"error": "Order has invalid price."}
        ]}}}
        self.sync()
        row = self.last_trade()
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["error_msg"], "Order has invalid price.")
        self.assertEqual(row["our_pos_after"], 0)
        self.assertNotIn("MANTA", trader._expected_pos)
        self.exchange.order.assert_called_once()

    def test_default_hip3_and_spot_precision(self):
        cases = [("MANTA", 0.06637085, 0.06637, 0.06638, 6),
                 ("BTC", 63451.234, 63451, 63452, 6),
                 ("xyz:SNDK", 0.06637085, 0.0663, 0.0664, 6),
                 ("@1", 0.0001234567, 0.0001234, 0.0001235, 8)]
        for coin, raw, buy, sell, max_decimals in cases:
            for is_buy, expected in [(True, buy), (False, sell)]:
                with self.subTest(coin=coin, is_buy=is_buy):
                    px = trader._round_price(raw, self.info, coin, is_buy)
                    self.assertEqual(px, expected)
                    self.assert_valid_price(px, self.info.asset_to_sz_decimals[self.info.coin_to_asset[coin]], max_decimals)

    def test_integer_exception_and_precision_boundaries(self):
        for raw in (0.00001234567, 0.00999999, 0.999999, 9.999999, 99.99999,
                    999.9999, 9999.999, 99999.99, 123456, 123456.7):
            for decimals in range(7):
                self.info.asset_to_sz_decimals[123] = decimals
                for is_buy in (True, False):
                    with self.subTest(raw=raw, sz_decimals=decimals, is_buy=is_buy):
                        if is_buy and raw < 10 ** -(6-decimals):
                            with self.assertRaisesRegex(ValueError, "rounds to zero"):
                                trader._round_price(raw, self.info, "MANTA", is_buy)
                            continue
                        result = trader._round_price(raw, self.info, "MANTA", is_buy)
                        self.assert_valid_price(result, decimals)
                        if is_buy:
                            self.assertLessEqual(result, raw)
                        else:
                            self.assertGreaterEqual(result, raw)
                        self.assertEqual(trader._round_price(result, self.info, "MANTA", is_buy), result)
        for is_buy in (True, False):
            self.assertEqual(trader._round_price(123456, self.info, "MANTA", is_buy), 123456)

    def test_invalid_price_or_metadata_never_submits(self):
        for raw in (float("nan"), float("inf"), -float("inf"), 0, -1, 0.0000001):
            with self.subTest(raw=raw):
                result = trader._place_order(self.exchange, self.info, "MANTA", True, 300, raw, 1)
                self.assertIsNone(result[0])
                self.assertTrue(result[4])
        for metadata in ({}, {123: -1}, {123: 7}, {123: "1"}, {123: True}):
            self.info.asset_to_sz_decimals = metadata
            result = trader._place_order(self.exchange, self.info, "MANTA", True, 300, 0.06637, 1)
            self.assertIsNone(result[0])
            self.assertIn("szDecimals", result[4])
        self.info.coin_to_asset = {}
        result = trader._place_order(self.exchange, self.info, "MANTA", True, 300, 0.06637, 1)
        self.assertIn("metadata", result[4])
        self.exchange.order.assert_not_called()
        self.exchange.update_leverage.assert_not_called()

    def test_place_order_defensively_normalizes_without_changing_size_or_ioc(self):
        result = trader._place_order(self.exchange, self.info, "MANTA", True, 300.14, 0.066371, 1)
        self.assertEqual(self.exchange.order.call_args.args, ("MANTA", True, 300.1, 0.06637, {"limit": {"tif": "Ioc"}}))
        self.assertEqual(self.exchange.order.call_args.kwargs, {"builder": None})
        self.assertEqual(result, ("42", 0.06539, 0.001, 300.1, None))

    def test_small_order_guard_and_force_close_preserved(self):
        result = trader._place_order(self.exchange, self.info, "MANTA", True, 1, 0.06637, 1)
        self.assertIn("below minimum", result[4])
        self.exchange.order.assert_not_called()
        result = trader._place_order(self.exchange, self.info, "MANTA", True, 1, 0.06637, 1, force=True)
        self.exchange.order.assert_called_once()
        self.assertEqual(result[0], "42")

    def test_zero_rounded_size_reports_local_reason(self):
        result = trader._place_order(self.exchange, self.info, "MANTA", True, 0.001, 0.06637, 1)
        self.assertIn("size is zero", result[4])
        self.exchange.order.assert_not_called()

    def test_order_exception_is_not_retried_or_recorded_as_confirmed_rejection(self):
        self.exchange.order.side_effect = TimeoutError("unknown submission result")
        with self.assertRaises(TimeoutError):
            self.sync()
        self.exchange.order.assert_called_once()
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM trades").fetchone()[0], 0)
        self.assertNotIn("MANTA", trader._expected_pos)

    def test_top_level_error_is_persisted(self):
        self.exchange.order.return_value = {"status": "err", "response": "Invalid order request"}
        self.sync()
        self.assertEqual(self.last_trade()["error_msg"], "Invalid order request")

    def test_nonfill_responses_have_diagnostic_reason(self):
        for statuses, expected in [([], "missing order status"), ([{"resting": {"oid": 123}}], "unexpectedly resting"),
                                    ([{"unexpected": True}], "Unexpected exchange order status")]:
            self.exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": statuses}}}
            result = trader._place_order(self.exchange, self.info, "MANTA", True, 300, 0.06637, 1)
            self.assertIsNone(result[0])
            self.assertIn(expected, result[4])

    def run_order_path(self, path):
        position = {"MANTA": {"size": 300, "entry_px": 0.06539, "leverage": 1, "unrealized_pnl": -5}}
        if path == "delta":
            self.sync()
        elif path == "force_close":
            self.sync(size=0, our_positions=position)
        elif path == "inline_sltp":
            cfg.set_value("stop_loss_pct", 20)
            self.sync(our_positions=position)
        elif path in ("close_all", "periodic_sltp"):
            cfg.set_value("stop_loss_pct", 20)
            with patch.object(trader, "_build_clients", return_value=(self.exchange, self.info)), \
                 patch.object(trader, "_get_relevant_dexes", return_value=[""]), \
                 patch.object(trader, "_get_positions", return_value=(100, 100, position, {"": 100})), \
                 patch.object(trader, "_get_mids", return_value={"MANTA": "0.06539"}):
                if path == "close_all":
                    trader.close_all_positions()
                else:
                    trader.check_sl_tp_periodic("source-fixture", {})
        else:
            module = moss_ws if path == "ws_init" else moss_poller
            positions = {"MANTA": {"size": 3000, "entry_px": 0.06539, "leverage": 1, "symbol": "MANTAUSDC"}}
            client = Mock()
            client.get_account.return_value = {"account_value": 1000}
            client.get_positions.return_value = []
            with patch.object(module, "_normalize_moss_positions", return_value=positions), \
                 patch.object(module, "_build_clients", return_value=(self.exchange, self.info)), \
                 patch.object(module, "_get_positions", return_value=(100, 100, {}, {"": 100})), \
                 patch.object(module, "_get_mids", return_value={"MANTA": "0.06539"}):
                if path == "ws_init":
                    module._init_baseline_from_bootstrap({"account_state": {"account_value": 1000}}, "source-fixture", client)
                else:
                    module._init_moss_baseline(client, "source-fixture")

    def test_all_six_call_sites_persist_rejections_and_use_legal_prices(self):
        cfg.set_value("agent_protocol_report.enabled", True)
        for path in ("delta", "force_close", "inline_sltp", "close_all", "periodic_sltp", "ws_init", "poller_init"):
            with self.subTest(path=path):
                cfg.set_value("stop_loss_pct", 0)
                trader._expected_pos.clear()
                db._baseline_init_seen.clear()
                db.clear_baselines("source-fixture")
                self.exchange.order.reset_mock()
                self.exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": [
                    {"error": "Order has invalid price."}
                ]}}}
                self.run_order_path(path)
                self.exchange.order.assert_called_once()
                price = self.exchange.order.call_args.args[3]
                self.assert_valid_price(price, 1)
                row = self.last_trade()
                self.assertEqual(row["order_price"], price)
                self.assertEqual(row["status"], "rejected")
                self.assertEqual(row["error_msg"], "Order has invalid price.")
                self.assertIsNone(row["our_order_id"])
                self.assertNotIn("MANTA", trader._expected_pos)
                report = db.get_agent_protocol_report_by_trade_id(row["id"])
                event = json.loads(report["event_json"])
                self.assertEqual(event["payload"]["trade"]["error_message"], "Order has invalid price.")
                self.assertEqual(event["payload"]["status"], "error")

    def test_all_six_call_sites_keep_successful_fill_handling(self):
        for path in ("delta", "force_close", "inline_sltp", "close_all", "periodic_sltp", "ws_init", "poller_init"):
            with self.subTest(path=path):
                cfg.set_value("stop_loss_pct", 0)
                trader._expected_pos.clear()
                db._baseline_init_seen.clear()
                db.clear_baselines("source-fixture")
                self.exchange.order.reset_mock()
                self.run_order_path(path)
                self.exchange.order.assert_called_once()
                row = self.last_trade()
                self.assertEqual(row["status"], "filled")
                self.assertEqual(row["our_order_id"], "42")
                self.assertEqual(row["filled_price"], 0.06539)
                self.assertIsNone(row["error_msg"])
                self.assertEqual(row["order_price"], self.exchange.order.call_args.args[3])

    def test_repaired_price_serializes_with_sdk_wire_format(self):
        from hyperliquid.utils.signing import float_to_wire
        for coin in self.info.coin_to_asset:
            for is_buy in (True, False):
                raw = 63451.234 if coin == "BTC" else 0.06637085
                px = trader._round_price(raw, self.info, coin, is_buy)
                wire = float_to_wire(px)
                self.assertEqual(Decimal(wire), Decimal(str(px)))

    def test_delta_minimum_trade_amount_still_skips_without_order(self):
        self.sync(size=10)
        self.exchange.order.assert_not_called()
        self.assertEqual(self.last_trade()["status"], "skipped")
        self.assertIn("below minimum", self.last_trade()["error_msg"])


if __name__ == "__main__":
    unittest.main()
