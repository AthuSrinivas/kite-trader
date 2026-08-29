"""Zerodha Kite Connect v3 client — the execution leg of a watchlist run.

The agent pipeline produces a rating; this module turns a rating into a real
order. Transport (auth exchange, request signing, response/error parsing) is
delegated to Zerodha's own SDK, ``kiteconnect`` (PyPI: ``kiteconnect``,
imported as ``pykiteconnect`` upstream): everything in this file that talks to
``api.kite.trade`` goes through a ``KiteConnect`` instance rather than raw
``requests`` calls, so REST-contract changes get fixed upstream instead of here.

What ``kiteconnect`` does NOT give you, and what this file still owns:
session *lifecycle* — the 6 AM IST expiry, the on-disk cache, and the
"reuse cache -> exchange KITE_REQUEST_TOKEN -> interactive login" fallback
chain. The SDK only exposes the primitives (``generate_session``,
``set_access_token``); it has no opinion on caching or when a token is stale.

Auth, in Kite's own terms (kite.trade/docs/connect/v3/user/):

    1. Send the user to ``https://kite.zerodha.com/connect/login?v=3&api_key=``
    2. They return to the app's registered redirect URL with a ``request_token``
    3. Exchange that (plus ``api_secret``) via ``generate_session()``, which
       returns an ``access_token`` and sets it on the client
    4. Every later call carries ``Authorization: token <api_key>:<access_token>``

The sharp edge is step 4: Kite invalidates every access token at 6:00 AM IST
and offers no silent refresh, so somebody has to click through the login page
once a day. The token is therefore cached on disk (mode 0600) with its issue
time, reused until that 6 AM boundary, and re-minted interactively — or from
``KITE_REQUEST_TOKEN`` — once it lapses. An unattended cron run needs a token
minted for it that morning; it cannot mint one by itself.

Environment:
    KITE_API_KEY, KITE_API_SECRET   app credentials from the developer console
    KITE_ACCESS_TOKEN               optional; bypasses the cache and handshake
    KITE_REQUEST_TOKEN              optional; exchanged without prompting
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_ex

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Kite kills every access token at 6:00 AM IST, whenever it was issued.
_TOKEN_EXPIRY_HOUR = 6

# NSE/BSE equity session. Holidays are NOT modelled — this is a guard rail
# against a misfiring cron sending MARKET orders into a closed book at 3 AM,
# not a trading calendar.
_SESSION_OPEN = dtime(hour=9, minute=15)
_SESSION_CLOSE = dtime(hour=15, minute=30)

# Yahoo-style exchange suffixes (the form the watchlist and the analysis path
# use) mapped to Kite exchange codes.
_EXCHANGE_BY_SUFFIX = {"NS": "NSE", "BO": "BSE"}

# Exchange prefixes Kite accepts in an ``exchange:tradingsymbol`` key.
_KITE_EXCHANGES = frozenset({"NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX", "NCO"})

# NSE/BSE equities quote in 5-paise steps; an order priced off-tick is rejected.
DEFAULT_TICK_SIZE = 0.05

DEFAULT_SESSION_PATH = Path.home() / ".kite-trader" / "session.json"


class KiteError(RuntimeError):
    """An error returned by the Kite API, carrying its typed ``error_type``.

    ``error_type`` is the name of the ``kiteconnect.exceptions`` class Zerodha's
    SDK mapped the response to (``GeneralException``, ``OrderException``, ...),
    which mirrors Kite's own documented error taxonomy.
    """

    def __init__(self, message: str, error_type: str = "GeneralException", status_code: int = 0):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


class KiteAuthError(KiteError):
    """TokenException — the session expired or was never established."""


def to_kite_instrument(symbol: str, default_exchange: str = "NSE") -> str:
    """Map a watchlist symbol to Kite's ``exchange:tradingsymbol`` key.

        RELIANCE.NS   -> NSE:RELIANCE
        TATASTEEL.BO  -> BSE:TATASTEEL
        NSE:INFY      -> NSE:INFY      (already qualified, passed through)
        SBIN          -> NSE:SBIN      (bare symbol takes ``default_exchange``)

    Kite's docs are explicit that ``exchange:tradingsymbol`` — not the numeric
    ``instrument_token`` — is the stable identifier, because exchanges recycle
    tokens after derivative expiry. An unrecognised suffix raises rather than
    guessing, so a stray ``AAPL.US`` in the watchlist fails loudly instead of
    resolving to some same-named Indian scrip.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"not a symbol: {symbol!r}")

    s = symbol.strip().upper()

    if ":" in s:
        exchange, _, tradingsymbol = s.partition(":")
        if exchange not in _KITE_EXCHANGES:
            raise ValueError(f"unknown Kite exchange {exchange!r} in {symbol!r}")
        if not tradingsymbol:
            raise ValueError(f"missing trading symbol in {symbol!r}")
        return f"{exchange}:{tradingsymbol}"

    if "." in s:
        tradingsymbol, _, suffix = s.rpartition(".")
        exchange = _EXCHANGE_BY_SUFFIX.get(suffix)
        if exchange is None:
            raise ValueError(
                f"{symbol!r} carries suffix .{suffix}, which is not an Indian exchange; "
                f"Kite handles only {sorted(_EXCHANGE_BY_SUFFIX)}"
            )
        return f"{exchange}:{tradingsymbol}"

    return f"{default_exchange}:{s}"


def round_to_tick(price: float, tick: float = DEFAULT_TICK_SIZE) -> float:
    """Snap a price to the nearest exchange tick. Off-tick orders are rejected."""
    return round(round(price / tick) * tick, 2)


def market_is_open(now: datetime | None = None) -> bool:
    """True during equity hours (Mon-Fri, 09:15-15:30 IST).

    Exchange holidays are not modelled, so this can return True on a holiday;
    it reliably catches only nights and weekends.
    """
    now = (now or datetime.now(IST)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    return _SESSION_OPEN <= now.time() <= _SESSION_CLOSE


def token_expiry(issued_at: datetime) -> datetime:
    """The 6:00 AM IST boundary at which a token issued at ``issued_at`` dies."""
    local = issued_at.astimezone(IST)
    expiry = local.replace(hour=_TOKEN_EXPIRY_HOUR, minute=0, second=0, microsecond=0)
    if local >= expiry:
        expiry += timedelta(days=1)
    return expiry


@dataclass
class OrderResult:
    """Outcome of one order attempt — placed, or refused before it was sent."""

    instrument: str
    transaction_type: str
    quantity: int
    order_id: str | None = None
    error: str | None = None

    @property
    def placed(self) -> bool:
        return self.order_id is not None


class ZerodhaBroker:
    """Session lifecycle plus the calls we need, on top of ``kiteconnect.KiteConnect``."""

    def __init__(
        self,
        api_key: str,
        api_secret: str | None = None,
        access_token: str | None = None,
        session_path: Path | str = DEFAULT_SESSION_PATH,
        timeout: int = 15,
    ):
        if not api_key:
            raise ValueError("api_key is required (set KITE_API_KEY)")
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_path = Path(session_path)
        self.timeout = timeout
        self._kite = KiteConnect(api_key=api_key, timeout=timeout)
        self._access_token: str | None = None
        self.access_token = access_token  # property setter also primes self._kite

    @classmethod
    def from_env(cls, session_path: Path | str = DEFAULT_SESSION_PATH) -> ZerodhaBroker:
        return cls(
            api_key=os.environ.get("KITE_API_KEY", ""),
            api_secret=os.environ.get("KITE_API_SECRET"),
            access_token=os.environ.get("KITE_ACCESS_TOKEN") or None,
            session_path=session_path,
        )

    # ---------------------------------------------------------------- session

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        # Keep this wrapper's notion of the token and the underlying SDK
        # client's in lock-step, so every call site that does
        # ``self.access_token = ...`` (cache load, generate_session, logout)
        # automatically takes effect on the next ``self._kite`` call.
        self._access_token = value
        self._kite.set_access_token(value)

    @property
    def login_url(self) -> str:
        return self._kite.login_url()

    def ensure_session(self, interactive: bool = True) -> str:
        """Return a usable access token, minting one if the cached one has died.

        Preference order: a token supplied explicitly (constructor or
        ``KITE_ACCESS_TOKEN``), the on-disk cache while still inside its 6 AM
        IST window, ``KITE_REQUEST_TOKEN`` exchanged silently, then an
        interactive login. Whichever is chosen is verified with
        ``/user/profile``, so a token revoked early — logging in again
        elsewhere invalidates the previous one — surfaces here rather than
        halfway through placing orders.
        """
        if self.access_token is None:
            self.access_token = self._load_cached_token()

        if self.access_token is not None:
            try:
                self.profile()
                return self.access_token
            except KiteAuthError:
                logger.info("Cached Kite session rejected by the API; re-authenticating")
                self.access_token = None

        request_token = os.environ.get("KITE_REQUEST_TOKEN")
        if not request_token:
            if not interactive or not sys.stdin.isatty():
                raise KiteAuthError(
                    "No valid Kite access token. Kite tokens expire at 6:00 AM IST and "
                    "cannot be refreshed headlessly — run this interactively once today, "
                    "or set KITE_ACCESS_TOKEN / KITE_REQUEST_TOKEN.",
                    error_type="TokenException",
                )
            request_token = self._prompt_for_request_token()

        return self.generate_session(request_token)

    def generate_session(self, request_token: str) -> str:
        """Exchange a ``request_token`` for an access token, and cache it."""
        if not self.api_secret:
            raise ValueError(
                "api_secret is required to exchange a request token (set KITE_API_SECRET)"
            )
        data = self._call(self._kite.generate_session, request_token, self.api_secret)
        self.access_token = data["access_token"]
        self._cache_token(self.access_token, user_id=data.get("user_id"))
        logger.info("Kite session established for %s", data.get("user_id"))
        return self.access_token

    def invalidate_session(self) -> None:
        """Log the API session out and drop the cache."""
        if not self.access_token:
            return
        self._call(self._kite.invalidate_access_token)
        self.access_token = None
        self.session_path.unlink(missing_ok=True)

    def _prompt_for_request_token(self) -> str:
        print("\nKite login required (tokens expire daily at 6:00 AM IST).")
        print(f"  1. Open: {self.login_url}")
        print("  2. Log in, then copy the URL you are redirected to.")
        raw = input("Paste the redirect URL or the request_token: ").strip()
        if "request_token=" in raw:
            return raw.split("request_token=", 1)[1].split("&", 1)[0].strip()
        return raw

    def _load_cached_token(self) -> str | None:
        try:
            cached = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        if cached.get("api_key") != self.api_key:
            return None
        try:
            issued_at = datetime.fromisoformat(cached["issued_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if datetime.now(IST) >= token_expiry(issued_at):
            logger.info("Cached Kite token passed its 6:00 AM IST expiry")
            return None
        return cached.get("access_token")

    def _cache_token(self, access_token: str, user_id: str | None = None) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "api_key": self.api_key,
            "access_token": access_token,
            "user_id": user_id,
            "issued_at": datetime.now(IST).isoformat(),
        }
        # Create restricted, then write: this is a bearer credential for a live
        # brokerage account and must never be readable by group or world, not
        # even for the instant between creation and chmod.
        self.session_path.touch(mode=0o600, exist_ok=True)
        self.session_path.chmod(0o600)
        self.session_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- transport

    def _call(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Run a ``kiteconnect`` call, translating its exceptions into ours.

        ``kiteconnect`` raises a typed subclass of ``KiteException`` for every
        API-level error (matching Kite's own documented error taxonomy) but
        lets ``requests`` exceptions (timeouts, connection errors) escape
        unwrapped — both are normalised to :class:`KiteError` /
        :class:`KiteAuthError` here so callers only ever see this module's
        two exception types.
        """
        try:
            return fn(*args, **kwargs)
        except kite_ex.TokenException as exc:
            raise KiteAuthError(str(exc), error_type="TokenException", status_code=exc.code) from exc
        except kite_ex.KiteException as exc:
            raise KiteError(str(exc), error_type=type(exc).__name__, status_code=exc.code) from exc
        except requests.RequestException as exc:
            raise KiteError(f"Kite request failed: {exc}", error_type="NetworkException") from exc

    def _authed_call(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Like :meth:`_call`, but fails fast (no network call) without a token."""
        if not self.access_token:
            raise KiteAuthError(
                "No access token; call ensure_session() first", error_type="TokenException"
            )
        return self._call(fn, *args, **kwargs)

    # ------------------------------------------------------------- user/funds

    def profile(self) -> dict[str, Any]:
        return self._authed_call(self._kite.profile)

    def margins(self, segment: str = "equity") -> dict[str, Any]:
        return self._authed_call(self._kite.margins, segment)

    def available_cash(self, segment: str = "equity") -> float:
        """Usable balance for a segment — Kite's ``net`` figure under margins."""
        return float(self.margins(segment).get("net", 0.0))

    # -------------------------------------------------------------- portfolio

    def holdings(self) -> list[dict[str, Any]]:
        return self._authed_call(self._kite.holdings) or []

    def sellable_quantities(self) -> dict[str, int]:
        """Map ``exchange:tradingsymbol`` -> quantity sellable today.

        Free demat quantity plus T1 (bought yesterday, sellable today) minus
        whatever open sell orders have already blocked. Negatives floor to zero.
        """
        quantities: dict[str, int] = {}
        for holding in self.holdings():
            key = f"{holding['exchange']}:{holding['tradingsymbol']}"
            free = (
                int(holding.get("quantity", 0) or 0)
                + int(holding.get("t1_quantity", 0) or 0)
                - int(holding.get("used_quantity", 0) or 0)
            )
            quantities[key] = max(free, 0)
        return quantities

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return self._authed_call(self._kite.positions) or {"net": [], "day": []}

    # ------------------------------------------------------------ market data

    def ltp(self, instruments: list[str]) -> dict[str, float]:
        """Last traded price per instrument, in one call (Kite's limit: 1000).

        Kite omits instruments it has no data for rather than returning a null,
        so the result may lack keys the caller asked for — check before use.
        """
        if not instruments:
            return {}
        data = self._authed_call(self._kite.ltp, instruments) or {}
        return {key: float(value["last_price"]) for key, value in data.items()}

    # ----------------------------------------------------------------- orders

    def place_order(
        self,
        instrument: str,
        transaction_type: str,
        quantity: int,
        product: str = "CNC",
        order_type: str = "MARKET",
        variety: str = "regular",
        price: float | None = None,
        trigger_price: float | None = None,
        validity: str = "DAY",
        tag: str | None = None,
    ) -> str:
        """Place an order — returns the ``order_id``.

        An order id means the order reached Kite's OMS, not that it filled;
        poll :meth:`order_history` for the terminal status.
        """
        exchange, _, tradingsymbol = instrument.partition(":")
        return self._authed_call(
            self._kite.place_order,
            variety=variety,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=int(quantity),
            product=product,
            order_type=order_type,
            price=round_to_tick(price) if price is not None else None,
            trigger_price=round_to_tick(trigger_price) if trigger_price is not None else None,
            validity=validity,
            tag=tag[:20] if tag else None,  # Kite rejects tags longer than 20 chars
        )

    def orders(self) -> list[dict[str, Any]]:
        return self._authed_call(self._kite.orders) or []

    def order_history(self, order_id: str) -> list[dict[str, Any]]:
        return self._authed_call(self._kite.order_history, order_id) or []
