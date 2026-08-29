"""The only module that imports TradingAgents.

Everything upstream-facing is funnelled through here so that when
TauricResearch/TradingAgents changes, exactly one file needs attention. The
rest of this project depends on :class:`RatingEngine` and :class:`Rating`,
which are ours.

Upstream surface we rely on (all of it public, all of it in their README):

    tradingagents.default_config.DEFAULT_CONFIG            dict of settings
    tradingagents.graph.trading_graph.TradingAgentsGraph
        __init__(selected_analysts=, debug=, config=)
        propagate(company_name, trade_date) -> (final_state, rating)
        save_reports(final_state, ticker) -> Path            [optional]

``propagate`` returns the full graph state plus a 5-tier rating string
(Buy / Overweight / Hold / Underweight / Sell). We verify that shape on every
call rather than trusting it, because a silent change upstream would otherwise
show up as a mystery HOLD — the worst kind of failure for something wired to a
brokerage account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ratings TradingAgents is documented to emit. Used only to warn on drift; the
# rating -> action mapping in watchlist.py is what actually gates trading.
KNOWN_RATINGS = frozenset({"buy", "overweight", "hold", "underweight", "sell"})

DEFAULT_ANALYSTS = ("market", "social", "news", "fundamentals")


class UpstreamError(RuntimeError):
    """TradingAgents is missing, or no longer matches the API we depend on."""


@dataclass
class Rating:
    """One symbol's verdict, decoupled from whatever shape upstream returned."""

    symbol: str
    trade_date: str
    rating: str
    decision_text: str = ""
    report_dir: Path | None = None
    error: str | None = None
    raw_state: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None


class RatingEngine:
    """Wraps ``TradingAgentsGraph`` and hands back a :class:`Rating` per symbol.

    The graph is built once and reused across the watchlist: construction spins
    up LLM clients and tool nodes, which is wasted work per symbol, and upstream
    already scopes per-ticker state inside ``propagate``.
    """

    def __init__(
        self,
        analysts: tuple[str, ...] = DEFAULT_ANALYSTS,
        config_overrides: dict[str, Any] | None = None,
        debug: bool = False,
        save_reports: bool = True,
    ):
        self.analysts = tuple(analysts)
        self.save_reports = save_reports
        self.debug = debug

        try:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph
        except ImportError as exc:
            raise UpstreamError(
                "Cannot import tradingagents. Install the upstream clone in this "
                "venv:  pip install -e /home/athu/TradingAgents"
            ) from exc

        # DEFAULT_CONFIG already folds in the TRADINGAGENTS_* env vars, so model
        # and provider choices live in .env rather than in this code.
        self.config = dict(DEFAULT_CONFIG)
        if config_overrides:
            self.config.update(config_overrides)

        try:
            self._graph = TradingAgentsGraph(
                selected_analysts=self.analysts, debug=debug, config=self.config
            )
        except TypeError as exc:
            raise UpstreamError(
                f"TradingAgentsGraph no longer accepts the arguments we pass ({exc}). "
                "Upstream changed its constructor; update kite_trader/agents.py."
            ) from exc

        if not hasattr(self._graph, "propagate"):
            raise UpstreamError(
                "TradingAgentsGraph has no .propagate() — upstream changed its API; "
                "update kite_trader/agents.py."
            )

    def rate(self, symbol: str, trade_date: str) -> Rating:
        """Run the full agent pipeline for one symbol.

        Never raises for a per-symbol failure: a broken ticker returns a Rating
        carrying ``error``, so one bad symbol cannot abandon the rest of the
        watchlist partway through.
        """
        logger.info("Analysing %s for %s", symbol, trade_date)
        try:
            result = self._graph.propagate(symbol, trade_date)
        except Exception as exc:  # noqa: BLE001 - one symbol must not sink the run
            logger.exception("Analysis failed for %s", symbol)
            return Rating(
                symbol=symbol,
                trade_date=trade_date,
                rating="Hold",
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            final_state, rating = result
        except (TypeError, ValueError):
            return Rating(
                symbol=symbol,
                trade_date=trade_date,
                rating="Hold",
                error=(
                    f"propagate() returned {type(result).__name__}, expected a "
                    "(state, rating) pair — upstream API changed; "
                    "update kite_trader/agents.py"
                ),
            )

        rating = str(rating).strip()
        if rating.lower() not in KNOWN_RATINGS:
            logger.warning(
                "%s: upstream returned unfamiliar rating %r; it will be treated as HOLD",
                symbol,
                rating,
            )

        report_dir = None
        if self.save_reports:
            report_dir = self._save_reports(final_state, symbol)

        return Rating(
            symbol=symbol,
            trade_date=trade_date,
            rating=rating,
            decision_text=str(final_state.get("final_trade_decision", "")),
            report_dir=report_dir,
            raw_state=final_state,
        )

    def _save_reports(self, final_state: dict[str, Any], symbol: str) -> Path | None:
        """Write upstream's markdown report tree; never fatal if it fails.

        Reports are an audit trail, not part of the decision, so a failure here
        is logged and the trade proceeds on the rating we already have.
        """
        writer = getattr(self._graph, "save_reports", None)
        if writer is None:
            return None
        try:
            return Path(writer(final_state, symbol))
        except Exception:  # noqa: BLE001 - reports are nice-to-have
            logger.warning("Could not write reports for %s", symbol, exc_info=True)
            return None


def upstream_version() -> str:
    """Report the installed TradingAgents version, for the run record."""
    try:
        from importlib.metadata import version

        return version("tradingagents")
    except Exception:  # noqa: BLE001
        return "unknown"
