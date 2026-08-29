# kite-trader

Runs the [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent
pipeline over a watchlist of Indian equities and routes each verdict to
Zerodha via [Kite Connect v3](https://kite.trade/docs/connect/v3/).

**Dry run is the default. Nothing reaches the exchange without `--live`.**

## Why this is a separate project

TradingAgents is consumed as an installed library and is **never edited**. Upgrading
is just `git pull` in the upstream clone — there is nothing here to merge, rebase,
or re-apply.

All upstream contact is funnelled through one file, `kite_trader/agents.py`, which
depends on three documented names:

| Upstream name | Used for |
|---|---|
| `DEFAULT_CONFIG` | base settings (already folds in `TRADINGAGENTS_*` env vars) |
| `TradingAgentsGraph(selected_analysts=, debug=, config=)` | building the graph |
| `.propagate(symbol, date) -> (state, rating)` | one symbol's verdict |

That module validates the shape it gets back on every call and raises `UpstreamError`
with a pointer to itself if upstream drifts — so an API change surfaces as one clear
message in one known file, not as a mystery HOLD on a live account.

Currently pinned against upstream `a33fd4c` (2026-07-18), TradingAgents v0.3.1.

## Layout

```
~/TradingAgents/          upstream clone — read-only, git pull freely
~/kite-trader/            this project
  kite_trader/
    agents.py             the ONLY module that imports tradingagents
    zerodha.py            Kite Connect v3 client (session, funds, quotes, orders)
    watchlist.py          watchlist parsing + rating -> BUY/SELL/HOLD
    runner.py             orchestration and CLI
  watchlist.txt           your symbols
  runs/                   JSON record of every run (git-ignored)
```

## Setup

```bash
cd ~/kite-trader
python3.12 -m venv .venv
.venv/bin/pip install -e ~/TradingAgents -e ".[dev]"
cp .env.example .env      # then fill in the keys
```

You need a Zerodha account with TOTP 2FA and a Kite Connect app
(https://developers.kite.trade) whose redirect URL is registered. Historical
candle data is a paid Kite add-on; this tool does not need it.

## Watchlist

```
RELIANCE.NS          # uses the run's default quantity
TCS.NS, 5            # fixed quantity for this symbol
INFY.NS  10          # comma or whitespace
NSE:SBIN             # already-qualified Kite keys work too
```

`.NS` -> NSE and `.BO` -> BSE. A symbol with any other suffix aborts the run before
a single LLM call, so a stray US ticker can never resolve to a same-named Indian scrip.

## Usage

```bash
kite-trader                                  # dry run over watchlist.txt
kite-trader --offline                        # analyse only, never contact Kite
kite-trader --symbols RELIANCE.NS,TCS.NS     # ad-hoc list
kite-trader --live                           # place orders, after confirmation
kite-trader --live --yes --max-orders 5      # unattended, hard-capped
kite-trader --live --variety amo             # queue for the next session
kite-trader --capital-per-trade 25000        # size by rupees instead of shares
kite-trader --order-type LIMIT               # marketable limit instead of market
```

## How a run works

1. **Authenticate with Kite first.** Analysis costs real money and takes minutes per
   symbol; finding a dead token afterwards would waste the whole run.
2. **Analyse every symbol.** A failure is recorded against that symbol and the rest
   of the watchlist continues.
3. **Plan** against fresh quotes, holdings and funds, then print the full table.
4. **Execute** — only with `--live`, and only after a typed confirmation.

Ratings map to orders as: Buy/Overweight -> BUY, Sell/Underweight -> SELL,
Hold -> no order. Anything unrecognised becomes HOLD, so a parse failure can never
open a position.

## Safety behaviour

- Dry run unless `--live`; live runs need a typed `yes` unless `--yes`.
- `--max-orders` (default 10) aborts the **entire** run if exceeded — it does not
  trim to the cap.
- SELL is capped at what you actually hold (free + T1 − blocked). This tool never
  shorts; a sell with no holding is skipped.
- BUY checks funds **cumulatively**, so several individually-affordable buys cannot
  overdraw the account between them.
- Regular orders are refused outside 09:15–15:30 IST (`--variety amo` or
  `--ignore-market-hours` to override). Exchange holidays are not modelled.
- Limit prices are snapped to the 0.05 tick; off-tick orders are rejected by Kite.
- Every run writes a JSON record to `runs/`.

## The daily-token problem

Kite invalidates every access token at **6:00 AM IST** and offers no silent refresh —
this is a Kite constraint, not a limitation of this tool. The token is cached at
`~/.kite-trader/session.json` (mode 0600) and reused until that boundary; after it,
the login page must be visited again.

So a fully unattended cron job is not possible on its own. The workable pattern is to
log in once each morning and export the token for the day:

```bash
kite-trader --offline --symbols RELIANCE.NS   # prompts for login, caches the token
# later, unattended:
kite-trader --live --yes --max-orders 5
```

Alternatively set `KITE_ACCESS_TOKEN` from a token minted earlier the same day.

## Tests

```bash
.venv/bin/pytest        # no network, no LLM calls, no broker
.venv/bin/ruff check .
```

## Scope

Equity delivery (CNC) on NSE/BSE. Orders are placed, not managed: an `order_id`
means the order reached Kite's OMS, not that it filled. There is no position
tracking, stop-loss, or exit logic — every run decides afresh from the agents'
current verdict.
