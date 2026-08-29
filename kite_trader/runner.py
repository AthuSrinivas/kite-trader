"""Run the agent pipeline across a watchlist and route the verdicts to Kite.

The run has four phases, in this order for a reason:

    1. Authenticate with Kite FIRST. The analysis phase costs real money in LLM
       calls and can take many minutes per symbol; discovering a dead token
       after all that would waste the whole run.
    2. Analyse every symbol. Failures are recorded per symbol, never fatal.
    3. Build the order plan against fresh quotes, holdings and funds, then show
       it in full. Sizing happens here, not during analysis, so prices are
       current at the moment orders go out.
    4. Execute — only with --live, and only after confirmation.

Dry run is the default. Nothing reaches the exchange unless --live is passed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .agents import RatingEngine, UpstreamError, upstream_version
from .watchlist import WatchlistEntry, parse_watchlist, rating_to_action
from .zerodha import (
    IST,
    KiteError,
    ZerodhaBroker,
    market_is_open,
    round_to_tick,
    to_kite_instrument,
)

logger = logging.getLogger("kite_trader")
console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WATCHLIST = PROJECT_ROOT / "watchlist.txt"
DEFAULT_RUN_DIR = PROJECT_ROOT / "runs"
VENDOR_TRADINGAGENTS = PROJECT_ROOT / "vendor" / "TradingAgents"


def _load_env_files() -> None:
    """Load this project's two separate .env files, by explicit path.

    Deliberately two files, not one, so each project only ever declares the
    keys it actually owns: this repo's .env holds KITE_* (TradingAgents has no
    use for a Kite credential), and vendor/TradingAgents/.env holds the LLM
    provider keys (kite_trader never reads an OPENAI_API_KEY itself - it's
    upstream's DEFAULT_CONFIG that consumes it). Explicit paths matter because
    ``tradingagents``'s own ``find_dotenv(usecwd=True)`` (run again, harmlessly,
    when it's imported later) only searches the current directory and its
    parents - it would never find a .env sitting in a subdirectory like
    vendor/TradingAgents/, so this project has to hand that one to it directly.
    ``load_dotenv`` defaults to ``override=False`` and no-ops on a missing
    path, so the order here is safe either way and neither file is required.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(VENDOR_TRADINGAGENTS / ".env")
    load_dotenv(VENDOR_TRADINGAGENTS / ".env.enterprise", override=False)

# Kite allows 10 orders/second; 150ms between placements stays well clear
# without making a watchlist run feel slow.
_ORDER_INTERVAL_SECONDS = 0.15


@dataclass
class Plan:
    """What we intend to do about one symbol, and what came of it."""

    symbol: str
    instrument: str | None = None
    rating: str = ""
    action: str = "HOLD"
    quantity: int = 0
    ltp: float | None = None
    price: float | None = None
    estimated_value: float | None = None
    status: str = "pending"  # planned | dry-run | placed | skipped | error
    reason: str = ""
    order_id: str | None = None
    report_dir: str | None = None

    @property
    def tradeable(self) -> bool:
        return self.action in ("BUY", "SELL") and self.quantity > 0 and self.status == "planned"


@dataclass
class RunRecord:
    """The full record of one run, written to disk as JSON."""

    started_at: str
    trade_date: str
    live: bool
    watchlist: str
    product: str
    order_type: str
    variety: str
    tradingagents_version: str
    plans: list[dict[str, Any]] = field(default_factory=list)
    finished_at: str | None = None


# --------------------------------------------------------------------- sizing


def size_order(
    entry: WatchlistEntry,
    ltp: float | None,
    default_qty: int,
    capital_per_trade: float | None,
) -> int:
    """Decide how many shares to trade.

    Precedence: the watchlist's per-symbol quantity, then a capital budget
    divided by the last traded price, then the run-wide default. A capital
    budget smaller than one share yields 0, which the caller reports as a skip
    rather than silently rounding up to a share it cannot fund.
    """
    if entry.quantity is not None:
        return entry.quantity
    if capital_per_trade is not None:
        if not ltp or ltp <= 0:
            return 0
        return int(capital_per_trade // ltp)
    return default_qty


def limit_price(action: str, ltp: float, buffer_pct: float) -> float:
    """A LIMIT price that crosses the spread by ``buffer_pct``, snapped to tick.

    Buys bid slightly above the last trade and sells offer slightly below, so
    the order is marketable rather than resting — a limit order that never
    fills leaves the agent's decision unexecuted, which is its own kind of risk.
    """
    factor = 1 + buffer_pct / 100 if action == "BUY" else 1 - buffer_pct / 100
    return round_to_tick(ltp * factor)


def build_plans(
    entries: list[WatchlistEntry],
    ratings: dict[str, Any],
    quotes: dict[str, float],
    sellable: dict[str, int],
    available_cash: float,
    args: argparse.Namespace,
) -> list[Plan]:
    """Turn ratings into concrete, funded, position-checked order intentions."""
    plans: list[Plan] = []
    remaining_cash = available_cash

    for entry in entries:
        rating = ratings[entry.symbol]
        plan = Plan(
            symbol=entry.symbol,
            rating=rating.rating,
            report_dir=str(rating.report_dir) if rating.report_dir else None,
        )

        if not rating.ok:
            plan.status = "error"
            plan.reason = rating.error or "analysis failed"
            plans.append(plan)
            continue

        plan.action = rating_to_action(rating.rating)
        plan.instrument = to_kite_instrument(entry.symbol)
        plan.ltp = quotes.get(plan.instrument)

        if plan.action == "HOLD":
            plan.status = "skipped"
            plan.reason = "rating is Hold"
            plans.append(plan)
            continue

        quantity = size_order(entry, plan.ltp, args.qty, args.capital_per_trade)

        if plan.action == "SELL":
            # Delivery sells can only dispose of stock actually held; this tool
            # does not short. Trim to the holding rather than sending an order
            # the exchange will reject.
            held = sellable.get(plan.instrument, 0)
            if held <= 0:
                plan.status = "skipped"
                plan.reason = "no holding to sell"
                plans.append(plan)
                continue
            if quantity > held:
                plan.reason = f"trimmed from {quantity} to holding of {held}"
                quantity = held

        if quantity <= 0:
            plan.status = "skipped"
            plan.reason = plan.reason or "computed quantity is 0"
            plans.append(plan)
            continue

        plan.quantity = quantity

        if plan.ltp:
            plan.estimated_value = round(plan.ltp * quantity, 2)
            if args.order_type == "LIMIT":
                plan.price = limit_price(plan.action, plan.ltp, args.limit_buffer_pct)
        elif args.order_type == "LIMIT":
            plan.status = "skipped"
            plan.reason = "no quote available to price a LIMIT order"
            plans.append(plan)
            continue

        if plan.action == "BUY" and not args.no_funds_check:
            cost = plan.estimated_value
            if cost is None:
                plan.status = "skipped"
                plan.reason = "no quote available to check funds"
                plans.append(plan)
                continue
            # Cumulative, not per-order: five affordable buys can still overdraw
            # the account between them.
            if cost > remaining_cash:
                plan.status = "skipped"
                plan.reason = f"needs Rs {cost:,.2f}, Rs {remaining_cash:,.2f} left"
                plans.append(plan)
                continue
            remaining_cash -= cost

        plan.status = "planned"
        plans.append(plan)

    return plans


# ------------------------------------------------------------------- printing


def print_plans(plans: list[Plan], live: bool) -> None:
    table = Table(
        title=f"Order plan — {'LIVE' if live else 'DRY RUN'}",
        header_style="bold",
        title_style="bold red" if live else "bold cyan",
    )
    table.add_column("Symbol")
    table.add_column("Rating")
    table.add_column("Action")
    table.add_column("Qty", justify="right")
    table.add_column("LTP", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Status")
    table.add_column("Note", overflow="fold")

    colours = {"BUY": "green", "SELL": "red", "HOLD": "dim"}
    for plan in plans:
        table.add_row(
            plan.symbol,
            plan.rating or "-",
            f"[{colours.get(plan.action, 'white')}]{plan.action}[/]",
            str(plan.quantity or "-"),
            f"{plan.ltp:,.2f}" if plan.ltp else "-",
            f"{plan.estimated_value:,.2f}" if plan.estimated_value else "-",
            plan.status,
            plan.reason or (plan.order_id or ""),
        )
    console.print(table)


def confirm(plans: list[Plan]) -> bool:
    """Require a typed confirmation before anything reaches the exchange."""
    tradeable = [p for p in plans if p.tradeable]
    buy_value = sum(p.estimated_value or 0 for p in tradeable if p.action == "BUY")

    console.print(
        f"\n[bold red]About to place {len(tradeable)} live order(s)[/] "
        f"— approximately Rs {buy_value:,.2f} of buying."
    )
    if not sys.stdin.isatty():
        console.print("[red]Not a terminal and --yes was not given; refusing to trade.[/]")
        return False
    return input("Type 'yes' to send these orders: ").strip().lower() == "yes"


# ------------------------------------------------------------------ execution


def execute(plans: list[Plan], broker: ZerodhaBroker, args: argparse.Namespace) -> None:
    tag = f"ta{datetime.now(IST):%y%m%d}"  # <=20 chars, groups a run in Kite's orderbook

    for plan in plans:
        if not plan.tradeable:
            continue
        try:
            plan.order_id = broker.place_order(
                instrument=plan.instrument,
                transaction_type=plan.action,
                quantity=plan.quantity,
                product=args.product,
                order_type=args.order_type,
                variety=args.variety,
                price=plan.price,
                tag=tag,
            )
            plan.status = "placed"
            console.print(
                f"  [green]placed[/] {plan.action} {plan.quantity} {plan.instrument} "
                f"-> order {plan.order_id}"
            )
        except KiteError as exc:
            plan.status = "error"
            plan.reason = f"{exc.error_type}: {exc}"
            console.print(f"  [red]failed[/] {plan.action} {plan.instrument}: {exc}")
        time.sleep(_ORDER_INTERVAL_SECONDS)


# ------------------------------------------------------------------------ cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kite-trader",
        description="Run TradingAgents over a watchlist and route BUY/SELL/HOLD to Zerodha.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST,
                        help="watchlist file")
    parser.add_argument("--symbols", help="comma-separated symbols, overriding the watchlist")
    parser.add_argument("--date", default=None,
                        help="analysis date YYYY-MM-DD (default: today, IST)")

    execution = parser.add_argument_group("execution")
    execution.add_argument("--live", action="store_true",
                           help="actually place orders (default is a dry run)")
    execution.add_argument("--yes", action="store_true",
                           help="skip the confirmation prompt (for cron; use with care)")
    execution.add_argument("--product", default="CNC", choices=["CNC", "MIS", "NRML", "MTF"])
    execution.add_argument("--order-type", default="MARKET", choices=["MARKET", "LIMIT"])
    execution.add_argument("--variety", default="regular", choices=["regular", "amo"],
                           help="'amo' queues orders for the next session")
    execution.add_argument("--limit-buffer-pct", type=float, default=0.3,
                           help="how far a LIMIT order crosses the spread")
    execution.add_argument("--max-orders", type=int, default=10,
                           help="hard cap on orders in one run; the run aborts if exceeded")
    execution.add_argument("--no-funds-check", action="store_true",
                           help="skip the cumulative cash check before buying")
    execution.add_argument("--ignore-market-hours", action="store_true",
                           help="place orders outside 09:15-15:30 IST anyway")

    sizing = parser.add_argument_group("sizing")
    sizing.add_argument("--qty", type=int, default=1,
                        help="default quantity when the watchlist gives none")
    sizing.add_argument("--capital-per-trade", type=float, default=None,
                        help="rupees per trade; quantity becomes floor(capital/LTP)")

    agents = parser.add_argument_group("agents")
    agents.add_argument("--analysts", default="market,social,news,fundamentals",
                        help="comma-separated analyst team")
    agents.add_argument("--no-save-reports", action="store_true",
                        help="skip writing the markdown report tree")
    agents.add_argument("--debug", action="store_true", help="stream agent messages")

    parser.add_argument("--offline", action="store_true",
                        help="analyse only; never contact Kite (implies a dry run)")
    parser.add_argument("--out", type=Path, default=DEFAULT_RUN_DIR,
                        help="directory for run records")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_env_files()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    logging.getLogger("kite_trader").setLevel(logging.DEBUG if args.debug else logging.INFO)

    if args.offline:
        args.live = False

    trade_date = args.date or datetime.now(IST).strftime("%Y-%m-%d")

    # ---- watchlist -------------------------------------------------------
    try:
        if args.symbols:
            entries = [WatchlistEntry(s.strip().upper()) for s in args.symbols.split(",") if s.strip()]
        else:
            entries = parse_watchlist(args.watchlist)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Watchlist error:[/] {exc}")
        return 2

    # Resolve every symbol to a Kite key up front: a typo should fail now, not
    # after an hour of paid analysis.
    try:
        for entry in entries:
            to_kite_instrument(entry.symbol)
    except ValueError as exc:
        console.print(f"[red]Symbol error:[/] {exc}")
        return 2

    console.print(
        f"[bold]{len(entries)} symbol(s)[/] for {trade_date} — "
        f"{'[red]LIVE[/]' if args.live else 'dry run'}"
    )

    # ---- phase 1: authenticate before spending money on analysis ---------
    broker = None
    if not args.offline:
        try:
            broker = ZerodhaBroker.from_env()
        except ValueError as exc:
            # Raised when KITE_API_KEY is absent — a setup mistake, not a crash.
            console.print(
                f"[red]Kite credentials missing:[/] {exc}\n"
                "Set KITE_API_KEY and KITE_API_SECRET in .env, or pass --offline to "
                "analyse without placing orders."
            )
            return 2
        try:
            broker.ensure_session()
            profile = broker.profile()
            console.print(f"Kite session OK — {profile.get('user_name')} ({profile.get('user_id')})")
        except (KiteError, ValueError) as exc:
            console.print(f"[red]Kite authentication failed:[/] {exc}")
            return 2

        if args.live and args.variety == "regular" and not market_is_open():
            if not args.ignore_market_hours:
                console.print(
                    "[red]Market is closed[/] (09:15-15:30 IST, Mon-Fri). "
                    "Use --variety amo to queue for the next session, "
                    "or --ignore-market-hours to override."
                )
                return 2
            console.print("[yellow]Market appears closed; proceeding as instructed.[/]")

    # ---- phase 2: analysis ----------------------------------------------
    try:
        engine = RatingEngine(
            analysts=tuple(a.strip() for a in args.analysts.split(",") if a.strip()),
            debug=args.debug,
            save_reports=not args.no_save_reports,
        )
    except UpstreamError as exc:
        console.print(f"[red]TradingAgents error:[/] {exc}")
        return 2

    ratings = {}
    for index, entry in enumerate(entries, start=1):
        console.rule(f"[{index}/{len(entries)}] {entry.symbol}")
        rating = engine.rate(entry.symbol, trade_date)
        ratings[entry.symbol] = rating
        if rating.ok:
            console.print(f"  -> [bold]{rating.rating}[/]")
        else:
            console.print(f"  -> [red]failed:[/] {rating.error}")

    # ---- phase 3: plan against fresh market state ------------------------
    quotes: dict[str, float] = {}
    sellable: dict[str, int] = {}
    cash = 0.0
    if broker is not None:
        instruments = [to_kite_instrument(e.symbol) for e in entries]
        try:
            quotes = broker.ltp(instruments)
            sellable = broker.sellable_quantities()
            cash = broker.available_cash()
            console.print(f"\nAvailable funds: Rs {cash:,.2f}")
        except KiteError as exc:
            console.print(f"[red]Could not read market state:[/] {exc}")
            return 2

    plans = build_plans(entries, ratings, quotes, sellable, cash, args)
    console.print()
    print_plans(plans, args.live)

    # ---- phase 4: execute ------------------------------------------------
    tradeable = [p for p in plans if p.tradeable]

    if args.live and tradeable:
        if len(tradeable) > args.max_orders:
            console.print(
                f"[red]{len(tradeable)} orders exceeds --max-orders={args.max_orders}.[/] "
                "Nothing was sent; raise the cap deliberately if this is expected."
            )
            return 2
        if args.yes or confirm(plans):
            console.print()
            execute(plans, broker, args)
        else:
            console.print("[yellow]Cancelled; no orders sent.[/]")
            for plan in tradeable:
                plan.status = "cancelled"
    elif tradeable:
        for plan in tradeable:
            plan.status = "dry-run"
        console.print(
            f"\n[cyan]Dry run:[/] {len(tradeable)} order(s) would be placed. "
            "Re-run with --live to send them."
        )
    else:
        console.print("\nNo orders to place.")

    # ---- record ----------------------------------------------------------
    record = RunRecord(
        started_at=datetime.now(IST).isoformat(),
        trade_date=trade_date,
        live=args.live,
        watchlist=str(args.watchlist),
        product=args.product,
        order_type=args.order_type,
        variety=args.variety,
        tradingagents_version=upstream_version(),
        plans=[asdict(p) for p in plans],
        finished_at=datetime.now(IST).isoformat(),
    )
    args.out.mkdir(parents=True, exist_ok=True)
    record_path = args.out / f"run_{datetime.now(IST):%Y%m%d_%H%M%S}.json"
    record_path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    console.print(f"Run record: {record_path}")

    return 1 if any(p.status == "error" for p in plans) else 0


if __name__ == "__main__":
    sys.exit(main())
