import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from kite_trader.zerodha import (
    IST,
    KiteAuthError,
    KiteError,
    ZerodhaBroker,
    market_is_open,
    round_to_tick,
    to_kite_instrument,
    token_expiry,
)

# ------------------------------------------------------------ symbol mapping

@pytest.mark.parametrize(
    "symbol,expected",
    [
        ("RELIANCE.NS", "NSE:RELIANCE"),
        ("reliance.ns", "NSE:RELIANCE"),
        ("TATASTEEL.BO", "BSE:TATASTEEL"),
        ("NSE:INFY", "NSE:INFY"),
        ("SBIN", "NSE:SBIN"),
    ],
)
def test_to_kite_instrument(symbol, expected):
    assert to_kite_instrument(symbol) == expected


@pytest.mark.parametrize("symbol", ["AAPL.US", "BMW.DE", "FOO:BAR", "NSE:", "", "  "])
def test_unmappable_symbols_raise(symbol):
    # Failing loudly beats resolving a foreign ticker to a same-named Indian scrip.
    with pytest.raises(ValueError):
        to_kite_instrument(symbol)


# -------------------------------------------------------------------- ticks

@pytest.mark.parametrize(
    "price,expected", [(100.02, 100.0), (100.03, 100.05), (2451.37, 2451.35), (0.04, 0.05)]
)
def test_round_to_tick(price, expected):
    assert round_to_tick(price) == expected


# ------------------------------------------------------------ token expiry

def test_token_issued_in_the_evening_expires_next_morning():
    issued = datetime(2026, 8, 28, 21, 0, tzinfo=IST)
    assert token_expiry(issued) == datetime(2026, 8, 29, 6, 0, tzinfo=IST)


def test_token_issued_after_midnight_expires_the_same_morning():
    issued = datetime(2026, 8, 29, 2, 30, tzinfo=IST)
    assert token_expiry(issued) == datetime(2026, 8, 29, 6, 0, tzinfo=IST)


def test_token_issued_at_six_expires_the_following_day():
    issued = datetime(2026, 8, 29, 6, 0, tzinfo=IST)
    assert token_expiry(issued) == datetime(2026, 8, 30, 6, 0, tzinfo=IST)


def test_cached_token_is_reused_within_its_window(tmp_path):
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "api_key": "key",
                "access_token": "live-token",
                "issued_at": datetime.now(IST).isoformat(),
            }
        )
    )
    broker = ZerodhaBroker("key", "secret", session_path=session)
    assert broker._load_cached_token() == "live-token"


def test_expired_cached_token_is_discarded(tmp_path):
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "api_key": "key",
                "access_token": "stale",
                "issued_at": (datetime.now(IST) - timedelta(days=2)).isoformat(),
            }
        )
    )
    assert ZerodhaBroker("key", "secret", session_path=session)._load_cached_token() is None


def test_cached_token_for_a_different_api_key_is_ignored(tmp_path):
    session = tmp_path / "session.json"
    session.write_text(
        json.dumps(
            {
                "api_key": "other-key",
                "access_token": "not-ours",
                "issued_at": datetime.now(IST).isoformat(),
            }
        )
    )
    assert ZerodhaBroker("key", "secret", session_path=session)._load_cached_token() is None


def test_cached_token_file_is_owner_only(tmp_path):
    session = tmp_path / "session.json"
    broker = ZerodhaBroker("key", "secret", session_path=session)
    broker._cache_token("secret-token", user_id="AB1234")
    assert session.stat().st_mode & 0o077 == 0, "token must not be group/world readable"


# ------------------------------------------------------------- transport

def _broker_with_response(status_code, payload, session_path):
    broker = ZerodhaBroker("key", "secret", access_token="token", session_path=session_path)
    response = MagicMock(status_code=status_code)
    response.json.return_value = payload
    broker._http = MagicMock()
    broker._http.request.return_value = response
    return broker


def test_token_exception_raises_the_recoverable_auth_error(tmp_path):
    broker = _broker_with_response(
        403,
        {"status": "error", "message": "Token expired", "error_type": "TokenException"},
        tmp_path / "s.json",
    )
    with pytest.raises(KiteAuthError) as exc:
        broker.profile()
    assert exc.value.error_type == "TokenException"


def test_other_errors_raise_plain_kite_error(tmp_path):
    broker = _broker_with_response(
        400,
        {"status": "error", "message": "Insufficient funds", "error_type": "MarginException"},
        tmp_path / "s.json",
    )
    with pytest.raises(KiteError) as exc:
        broker.profile()
    assert not isinstance(exc.value, KiteAuthError)
    assert exc.value.error_type == "MarginException"


def test_authorization_header_matches_kite_format(tmp_path):
    broker = _broker_with_response(200, {"status": "success", "data": {}}, tmp_path / "s.json")
    broker.profile()
    headers = broker._http.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "token key:token"


# ---------------------------------------------------------------- portfolio

def test_sellable_subtracts_blocked_and_adds_t1(tmp_path):
    broker = ZerodhaBroker("key", "secret", access_token="t", session_path=tmp_path / "s.json")
    broker.holdings = lambda: [
        {"exchange": "NSE", "tradingsymbol": "TCS", "quantity": 10, "t1_quantity": 5,
         "used_quantity": 3},
        {"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 2, "t1_quantity": 0,
         "used_quantity": 7},
    ]
    sellable = broker.sellable_quantities()
    assert sellable["NSE:TCS"] == 12
    assert sellable["NSE:INFY"] == 0, "an over-blocked holding must floor at zero, not go negative"


# ------------------------------------------------------------------- orders

def test_place_order_sends_the_documented_payload(tmp_path):
    broker = _broker_with_response(
        200, {"status": "success", "data": {"order_id": "2508290001"}}, tmp_path / "s.json"
    )
    order_id = broker.place_order(
        "NSE:RELIANCE", "BUY", 3, order_type="LIMIT", price=1402.4321, tag="ta260829"
    )
    assert order_id == "2508290001"

    call = broker._http.request.call_args
    assert call.args[0] == "POST"
    assert call.args[1].endswith("/orders/regular")
    assert call.kwargs["data"] == {
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "transaction_type": "BUY",
        "quantity": 3,
        "product": "CNC",
        "order_type": "LIMIT",
        "validity": "DAY",
        "price": 1402.45,  # snapped to the 0.05 tick
        "tag": "ta260829",
    }


def test_order_tag_is_truncated_to_kites_limit(tmp_path):
    broker = _broker_with_response(
        200, {"status": "success", "data": {"order_id": "1"}}, tmp_path / "s.json"
    )
    broker.place_order("NSE:TCS", "SELL", 1, tag="x" * 40)
    assert len(broker._http.request.call_args.kwargs["data"]["tag"]) == 20


def test_ltp_requests_all_instruments_in_one_call(tmp_path):
    broker = _broker_with_response(
        200,
        {"status": "success", "data": {
            "NSE:TCS": {"last_price": 3100.5},
            "NSE:INFY": {"last_price": 1450.0},
        }},
        tmp_path / "s.json",
    )
    assert broker.ltp(["NSE:TCS", "NSE:INFY"]) == {"NSE:TCS": 3100.5, "NSE:INFY": 1450.0}
    assert broker._http.request.call_count == 1


def test_ltp_omits_instruments_kite_had_no_data_for(tmp_path):
    broker = _broker_with_response(
        200, {"status": "success", "data": {"NSE:TCS": {"last_price": 3100.5}}}, tmp_path / "s.json"
    )
    assert broker.ltp(["NSE:TCS", "NSE:DELISTED"]) == {"NSE:TCS": 3100.5}


# ------------------------------------------------------------- market hours

@pytest.mark.parametrize(
    "when,expected",
    [
        (datetime(2026, 8, 28, 10, 0, tzinfo=IST), True),    # Friday mid-session
        (datetime(2026, 8, 28, 9, 0, tzinfo=IST), False),    # pre-open
        (datetime(2026, 8, 28, 16, 0, tzinfo=IST), False),   # after close
        (datetime(2026, 8, 29, 11, 0, tzinfo=IST), False),   # Saturday
        (datetime(2026, 8, 30, 11, 0, tzinfo=IST), False),   # Sunday
    ],
)
def test_market_is_open(when, expected):
    assert market_is_open(when) is expected
