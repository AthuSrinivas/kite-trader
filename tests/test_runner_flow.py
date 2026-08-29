"""End-to-end wiring of main(), with the LLM pipeline and Kite both mocked.

These prove the safety properties that matter: a dry run reaches no broker,
and a live run only sends what was planned and confirmed.
"""

from unittest.mock import MagicMock, patch

import pytest

from kite_trader.agents import Rating
from kite_trader.runner import main


@pytest.fixture
def watchlist(tmp_path):
    path = tmp_path / "watchlist.txt"
    path.write_text("TCS.NS, 2\nINFY.NS, 3\n", encoding="utf-8")
    return path


@pytest.fixture
def broker():
    """A Kite broker that answers every read and records every order."""
    mock = MagicMock()
    mock.api_key = "test-key"
    mock.profile.return_value = {"user_name": "Test User", "user_id": "AB1234"}
    mock.ltp.return_value = {"NSE:TCS": 3000.0, "NSE:INFY": 1500.0}
    mock.sellable_quantities.return_value = {"NSE:INFY": 10}
    mock.available_cash.return_value = 100_000.0
    mock.place_order.return_value = "2508290001"
    return mock


def run(args, broker, ratings, tmp_path):
    engine = MagicMock()
    engine.rate.side_effect = lambda symbol, date: Rating(
        symbol=symbol, trade_date=date, rating=ratings[symbol]
    )
    with (
        patch("kite_trader.runner.ZerodhaBroker") as broker_cls,
        patch("kite_trader.runner.RatingEngine", return_value=engine),
        patch("kite_trader.runner.market_is_open", return_value=True),
    ):
        broker_cls.from_env.return_value = broker
        code = main([*args, "--out", str(tmp_path / "runs")])
    return code


def test_dry_run_places_nothing(watchlist, broker, tmp_path):
    code = run(
        ["--watchlist", str(watchlist), "--no-save-reports"],
        broker,
        {"TCS.NS": "Buy", "INFY.NS": "Sell"},
        tmp_path,
    )
    assert code == 0
    broker.place_order.assert_not_called()


def test_live_run_places_the_planned_orders(watchlist, broker, tmp_path):
    code = run(
        ["--watchlist", str(watchlist), "--live", "--yes", "--no-save-reports"],
        broker,
        {"TCS.NS": "Buy", "INFY.NS": "Sell"},
        tmp_path,
    )
    assert code == 0
    assert broker.place_order.call_count == 2

    buy = broker.place_order.call_args_list[0].kwargs
    assert (buy["instrument"], buy["transaction_type"], buy["quantity"]) == ("NSE:TCS", "BUY", 2)
    assert buy["product"] == "CNC"
    assert buy["tag"].startswith("ta")

    sell = broker.place_order.call_args_list[1].kwargs
    assert (sell["instrument"], sell["transaction_type"], sell["quantity"]) == ("NSE:INFY", "SELL", 3)


def test_hold_ratings_produce_no_orders(watchlist, broker, tmp_path):
    code = run(
        ["--watchlist", str(watchlist), "--live", "--yes", "--no-save-reports"],
        broker,
        {"TCS.NS": "Hold", "INFY.NS": "Hold"},
        tmp_path,
    )
    assert code == 0
    broker.place_order.assert_not_called()


def test_max_orders_cap_aborts_the_whole_run(watchlist, broker, tmp_path):
    code = run(
        ["--watchlist", str(watchlist), "--live", "--yes", "--max-orders", "1",
         "--no-save-reports"],
        broker,
        {"TCS.NS": "Buy", "INFY.NS": "Sell"},
        tmp_path,
    )
    assert code == 2
    # The cap must block the whole run, not just trim the excess orders.
    broker.place_order.assert_not_called()


def test_a_rejected_order_does_not_stop_the_rest(watchlist, broker, tmp_path):
    from kite_trader.zerodha import KiteError

    broker.place_order.side_effect = [
        KiteError("Insufficient funds", error_type="MarginException"),
        "2508290002",
    ]
    code = run(
        ["--watchlist", str(watchlist), "--live", "--yes", "--no-save-reports"],
        broker,
        {"TCS.NS": "Buy", "INFY.NS": "Sell"},
        tmp_path,
    )
    assert code == 1, "a failed order should surface as a non-zero exit"
    assert broker.place_order.call_count == 2


def test_offline_never_touches_the_broker(watchlist, broker, tmp_path):
    code = run(
        ["--watchlist", str(watchlist), "--offline", "--no-save-reports"],
        broker,
        {"TCS.NS": "Buy", "INFY.NS": "Sell"},
        tmp_path,
    )
    assert code == 0
    broker.ensure_session.assert_not_called()
    broker.place_order.assert_not_called()


def test_a_bad_symbol_aborts_before_any_llm_spend(watchlist, broker, tmp_path):
    engine = MagicMock()
    with (
        patch("kite_trader.runner.ZerodhaBroker") as broker_cls,
        patch("kite_trader.runner.RatingEngine", return_value=engine) as engine_cls,
    ):
        broker_cls.from_env.return_value = broker
        code = main(["--symbols", "AAPL.US", "--out", str(tmp_path / "runs")])

    assert code == 2
    # Symbol validation must precede any paid LLM analysis.
    engine_cls.assert_not_called()


def test_run_record_is_written(watchlist, broker, tmp_path):
    run(["--watchlist", str(watchlist), "--no-save-reports"], broker,
        {"TCS.NS": "Buy", "INFY.NS": "Hold"}, tmp_path)
    records = list((tmp_path / "runs").glob("run_*.json"))
    assert len(records) == 1


def test_missing_credentials_reports_setup_error_not_a_traceback(watchlist, tmp_path):
    with patch("kite_trader.runner.ZerodhaBroker") as broker_cls:
        broker_cls.from_env.side_effect = ValueError("api_key is required (set KITE_API_KEY)")
        code = main(["--watchlist", str(watchlist), "--out", str(tmp_path / "runs")])
    assert code == 2
