"""The sizing and safety logic — the part that decides what actually gets sent."""

import argparse

import pytest

from kite_trader.agents import Rating
from kite_trader.runner import build_plans, limit_price, size_order
from kite_trader.watchlist import WatchlistEntry


def make_args(**overrides):
    defaults = {
        "qty": 1,
        "capital_per_trade": None,
        "order_type": "MARKET",
        "limit_buffer_pct": 0.3,
        "no_funds_check": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def rating(symbol, value="Buy", error=None):
    return Rating(symbol=symbol, trade_date="2026-08-29", rating=value, error=error)


# --------------------------------------------------------------------- sizing

def test_watchlist_quantity_wins_over_everything():
    entry = WatchlistEntry("TCS.NS", 7)
    assert size_order(entry, ltp=100.0, default_qty=1, capital_per_trade=100_000) == 7


def test_capital_budget_divides_by_price():
    entry = WatchlistEntry("TCS.NS")
    assert size_order(entry, ltp=3000.0, default_qty=1, capital_per_trade=10_000) == 3


def test_capital_smaller_than_one_share_yields_zero():
    # Must not round up into a share the account cannot fund.
    entry = WatchlistEntry("TCS.NS")
    assert size_order(entry, ltp=3000.0, default_qty=1, capital_per_trade=500) == 0


def test_capital_sizing_without_a_quote_yields_zero():
    entry = WatchlistEntry("TCS.NS")
    assert size_order(entry, ltp=None, default_qty=5, capital_per_trade=10_000) == 0


def test_default_quantity_is_the_fallback():
    assert size_order(WatchlistEntry("TCS.NS"), ltp=3000.0, default_qty=4, capital_per_trade=None) == 4


# ---------------------------------------------------------------- limit price

def test_limit_prices_cross_the_spread_in_the_right_direction():
    assert limit_price("BUY", 1000.0, 0.3) > 1000.0
    assert limit_price("SELL", 1000.0, 0.3) < 1000.0


def test_limit_price_is_snapped_to_tick():
    price = limit_price("BUY", 1402.0, 0.3)
    assert round(price / 0.05) * 0.05 == pytest.approx(price)


# ------------------------------------------------------------------ planning

def test_hold_never_produces_an_order():
    entries = [WatchlistEntry("TCS.NS")]
    plans = build_plans(entries, {"TCS.NS": rating("TCS.NS", "Hold")},
                        {"NSE:TCS": 3000.0}, {}, 100_000, make_args())
    assert plans[0].action == "HOLD"
    assert plans[0].status == "skipped"
    assert not plans[0].tradeable


def test_sell_without_a_holding_is_skipped_not_shorted():
    plans = build_plans([WatchlistEntry("TCS.NS")], {"TCS.NS": rating("TCS.NS", "Sell")},
                        {"NSE:TCS": 3000.0}, {}, 100_000, make_args())
    assert plans[0].status == "skipped"
    assert "no holding" in plans[0].reason


def test_sell_is_trimmed_to_the_holding():
    plans = build_plans([WatchlistEntry("TCS.NS", 50)], {"TCS.NS": rating("TCS.NS", "Sell")},
                        {"NSE:TCS": 3000.0}, {"NSE:TCS": 12}, 0, make_args())
    assert plans[0].quantity == 12
    assert plans[0].tradeable


def test_buy_beyond_available_cash_is_skipped():
    plans = build_plans([WatchlistEntry("TCS.NS", 10)], {"TCS.NS": rating("TCS.NS", "Buy")},
                        {"NSE:TCS": 3000.0}, {}, 5_000, make_args())
    assert plans[0].status == "skipped"
    assert "needs" in plans[0].reason


def test_funds_check_is_cumulative_across_buys():
    # Each buy is individually affordable; together they overdraw the account.
    entries = [WatchlistEntry("TCS.NS", 1), WatchlistEntry("INFY.NS", 1), WatchlistEntry("SBIN.NS", 1)]
    ratings = {e.symbol: rating(e.symbol, "Buy") for e in entries}
    quotes = {"NSE:TCS": 3000.0, "NSE:INFY": 3000.0, "NSE:SBIN": 3000.0}
    plans = build_plans(entries, ratings, quotes, {}, 7_000, make_args())

    assert [p.status for p in plans] == ["planned", "planned", "skipped"]
    assert "left" in plans[2].reason


def test_no_funds_check_flag_lets_buys_through():
    plans = build_plans([WatchlistEntry("TCS.NS", 10)], {"TCS.NS": rating("TCS.NS", "Buy")},
                        {"NSE:TCS": 3000.0}, {}, 0, make_args(no_funds_check=True))
    assert plans[0].tradeable


def test_failed_analysis_becomes_an_error_row_not_a_trade():
    plans = build_plans([WatchlistEntry("TCS.NS")],
                        {"TCS.NS": rating("TCS.NS", "Buy", error="LLM timeout")},
                        {"NSE:TCS": 3000.0}, {}, 100_000, make_args())
    assert plans[0].status == "error"
    assert not plans[0].tradeable


def test_limit_order_without_a_quote_is_skipped():
    plans = build_plans([WatchlistEntry("TCS.NS", 1)], {"TCS.NS": rating("TCS.NS", "Buy")},
                        {}, {}, 100_000, make_args(order_type="LIMIT"))
    assert plans[0].status == "skipped"
    assert "LIMIT" in plans[0].reason


def test_market_buy_without_a_quote_is_skipped_by_the_funds_check():
    plans = build_plans([WatchlistEntry("TCS.NS", 1)], {"TCS.NS": rating("TCS.NS", "Buy")},
                        {}, {}, 100_000, make_args())
    assert plans[0].status == "skipped"


def test_overweight_and_underweight_reach_the_broker():
    entries = [WatchlistEntry("TCS.NS", 1), WatchlistEntry("INFY.NS", 1)]
    ratings = {"TCS.NS": rating("TCS.NS", "Overweight"), "INFY.NS": rating("INFY.NS", "Underweight")}
    plans = build_plans(entries, ratings, {"NSE:TCS": 100.0, "NSE:INFY": 100.0},
                        {"NSE:INFY": 5}, 100_000, make_args())
    assert plans[0].action == "BUY" and plans[0].tradeable
    assert plans[1].action == "SELL" and plans[1].tradeable
