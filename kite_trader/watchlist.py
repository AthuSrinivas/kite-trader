"""Watchlist parsing and the rating -> order-action mapping.

Kept separate from the runner so both halves are unit-testable without an LLM
or a live broker session.

Watchlist file format — one instrument per line::

    # Nifty large caps
    RELIANCE.NS          # quantity falls back to the run's default
    TCS.NS, 5            # explicit quantity for this symbol
    INFY.NS  10          # comma or whitespace, either works
    NSE:SBIN             # already-qualified Kite keys are accepted too

Blank lines and ``#`` comments (whole-line or trailing) are ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# TradingAgents emits a 5-tier rating (tradingagents.agents.utils.rating); a
# broker understands three verbs. Overweight/Underweight collapse into their
# directional neighbours — a conviction tilt is still a trade — while anything
# unrecognised collapses to HOLD, so a parse miss can never open a position.
_ACTION_BY_RATING = {
    "buy": "BUY",
    "overweight": "BUY",
    "hold": "HOLD",
    "underweight": "SELL",
    "sell": "SELL",
}

ACTIONS = ("BUY", "SELL", "HOLD")


@dataclass(frozen=True)
class WatchlistEntry:
    """One line of the watchlist: a symbol and an optional per-symbol quantity."""

    symbol: str
    quantity: int | None = None


def rating_to_action(rating: str) -> str:
    """Map a 5-tier rating to BUY / SELL / HOLD. Unknown ratings become HOLD."""
    action = _ACTION_BY_RATING.get(str(rating).strip().lower())
    if action is None:
        logger.warning("Unrecognised rating %r; treating as HOLD", rating)
        return "HOLD"
    return action


def parse_watchlist(path: Path | str) -> list[WatchlistEntry]:
    """Read a watchlist file into entries, preserving file order.

    Raises ``FileNotFoundError`` if the file is missing and ``ValueError`` on a
    malformed line — a typo'd quantity should stop the run, not quietly trade
    the default size.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    entries: list[WatchlistEntry] = []
    seen: set[str] = set()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        parts = line.replace(",", " ").split()
        symbol = parts[0].upper()

        if len(parts) > 2:
            raise ValueError(f"{path}:{lineno}: expected 'SYMBOL [quantity]', got {raw.strip()!r}")

        quantity: int | None = None
        if len(parts) == 2:
            try:
                quantity = int(parts[1])
            except ValueError:
                raise ValueError(
                    f"{path}:{lineno}: quantity {parts[1]!r} is not an integer"
                ) from None
            if quantity <= 0:
                raise ValueError(f"{path}:{lineno}: quantity must be positive, got {quantity}")

        if symbol in seen:
            logger.warning("%s:%d: duplicate symbol %s ignored", path, lineno, symbol)
            continue

        seen.add(symbol)
        entries.append(WatchlistEntry(symbol=symbol, quantity=quantity))

    if not entries:
        raise ValueError(f"{path} contains no symbols")

    return entries
