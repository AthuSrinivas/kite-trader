import pytest

from kite_trader.watchlist import WatchlistEntry, parse_watchlist, rating_to_action


def write(tmp_path, text):
    path = tmp_path / "watchlist.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_parses_symbols_comments_and_quantities(tmp_path):
    path = write(
        tmp_path,
        """
        # a comment
        RELIANCE.NS
        TCS.NS, 5
        INFY.NS  10      # trailing comment
        NSE:SBIN
        """.replace("        ", ""),
    )
    assert parse_watchlist(path) == [
        WatchlistEntry("RELIANCE.NS", None),
        WatchlistEntry("TCS.NS", 5),
        WatchlistEntry("INFY.NS", 10),
        WatchlistEntry("NSE:SBIN", None),
    ]


def test_duplicates_are_dropped_keeping_first(tmp_path):
    path = write(tmp_path, "TCS.NS, 5\ntcs.ns, 9\n")
    assert parse_watchlist(path) == [WatchlistEntry("TCS.NS", 5)]


@pytest.mark.parametrize("line", ["TCS.NS abc", "TCS.NS 0", "TCS.NS -3", "TCS.NS 1 2"])
def test_malformed_lines_raise_rather_than_defaulting(tmp_path, line):
    # A typo'd quantity must stop the run, not silently trade the default size.
    with pytest.raises(ValueError):
        parse_watchlist(write(tmp_path, line))


def test_empty_watchlist_raises(tmp_path):
    with pytest.raises(ValueError):
        parse_watchlist(write(tmp_path, "# nothing here\n\n"))


@pytest.mark.parametrize(
    "rating,action",
    [
        ("Buy", "BUY"),
        ("Overweight", "BUY"),
        ("Hold", "HOLD"),
        ("Underweight", "SELL"),
        ("Sell", "SELL"),
        ("  sell  ", "SELL"),
    ],
)
def test_rating_maps_to_action(rating, action):
    assert rating_to_action(rating) == action


@pytest.mark.parametrize("rating", ["Strong Buy", "", "???", None])
def test_unknown_ratings_never_trade(rating):
    assert rating_to_action(rating) == "HOLD"
