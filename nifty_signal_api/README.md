# Nifty Signal API (Fyers)

A Flask REST API that pulls live Nifty data from the Fyers API v3 and returns:

- support and resistance levels (pivots, CPR, previous day high/low, opening range, option-chain OI walls)
- market bias (bullish, bearish or neutral) with the reasons
- an entry, stop-loss and two targets on the Nifty index
- the Nifty option to trade (CE or PE), with the full instrument details: Fyers symbol, fyToken, strike, expiry, lot size, LTP, bid/ask, OI, greeks, and estimated premium at entry, stop-loss and targets

It also serves a **simple web page** at `http://127.0.0.1:5110/`. The page shows the signal in plain words, the trade plan, the option to buy (with a Copy button for the symbol), the support and resistance ladder, and why the signal fired. It refreshes every 30 seconds.

It also has:
- **4 selectable strategies:** Confluence, Opening Range Breakout, EMA + VWAP Trend and OI Wall Bounce. Their rules are shown on the page.
- **Paper trading:** trade any signal with virtual money, track live P&L, and compare results by strategy.

- **Auto paper trading:** from 9:15 to market close it runs **all strategies on live data**, paper-trades every signal with a **₹100 crore** virtual account, and shows everything in a **live log** on the page.

- **Live market board:**
  - all **Nifty 50 stocks** with live price, change, weight and the index points each stock added today
  - VWAP, volume and traded value
  - **buy side / sell side** per stock
  - top gainers and losers, the stocks pushing Nifty up or dragging it down, and market breadth
  - a clickable **order book** for any stock or option
  - **most active Nifty options** with OI change and buildup (long buildup, short buildup, short covering, long unwinding)

It never sends real orders to the exchange. It never places orders, so SEBI's static-IP rule for API orders does not apply.

> Rule-based technical levels, not predictions or investment advice.

## 1. Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # then fill in your Fyers app details
python app.py               # runs on http://0.0.0.0:5110
```

**Create a Fyers API app** at https://myapi.fyers.in/dashboard:
- Set the **Redirect URL** to `http://127.0.0.1:5110/auth/callback`. If you run this on a server, use the server's address. It must exactly match `FYERS_REDIRECT_URI` in `.env`.
- Copy the **App ID** (e.g. `XB12345-100`) into `FYERS_CLIENT_ID` and the **Secret ID** into `FYERS_SECRET_KEY`.

**Try it without Fyers (demo mode):** `python demo.py` (or `python demo.py down` / `python demo.py flat`), then open http://127.0.0.1:5110. It uses simulated, moving prices (not real ones) and runs the auto-trader with market hours ignored, so you can watch it trade straight away.

## 2. Log in once every trading day

Fyers access tokens expire daily.

1. Open the web page and click **Log in to Fyers**. (Or call `GET /auth/login-url` and open the `login_url` it returns.)
2. Log in to Fyers. You'll be sent back to the page, and today's token is saved to `fyers_token.json`.

Until you log in, `/api/*` calls return `401` with `"login_url": "/auth/login-url"`.

## 3. Endpoints

| Method & path | What it returns |
|---|---|
| `GET /` | The web page |
| `GET /health` | Service status and whether Fyers is logged in |
| `GET /auth/login-url` | The Fyers login link |
| `GET /auth/callback` | Fyers redirects here (saves the token) |
| `GET /api/nifty/signal` | Full report: levels, bias, entry/SL/targets, option instrument |
| `GET /api/nifty/levels` | Support/resistance, pivots and OI summary only |
| `GET /api/nifty/option-chain` | Full Nifty option chain grouped by strike |
| `GET /api/nifty/expiries` | Available expiries (date and epoch) |
| `GET /api/strategies` | The strategies, with rules and when each works best |
| `GET /api/paper/summary` | Paper account, open positions, history and stats by strategy |
| `POST /api/paper/trades` | Paper trade the current signal (see section 5) |
| `POST /api/paper/trades/<id>/close` | Exit an open paper trade at the live price, or cancel a pending one |
| `POST /api/paper/reset` | Reset the paper account, e.g. `{"capital": 100000}` |

`/api/nifty/signal` parameters:
- `strategy` = `confluence` (default), `orb`, `ema_vwap`, `oi_bounce`
- `moneyness` = `ATM` (default), `ITM1`, `ITM2`, `OTM1`, `OTM2`
- `expiry` = an epoch value from `/api/nifty/expiries` (default: nearest expiry)

`/api/nifty/option-chain` parameters: `strikecount` (1–50, default 15) and `expiry`.

If `API_KEY` is set in `.env`, send it as the header `X-API-Key` on every `/api/*` call.

```bash
curl "http://127.0.0.1:5110/api/nifty/signal?moneyness=ATM"
```

`sample_response.json` shows a full example response (made from simulated data).

## 4. How the numbers are worked out

**Levels**
- Classic pivots (P, R1–R3, S1–S3) and CPR from the previous day's high, low and close.
- Previous day high/low, today's high/low, and the opening range (first 15 minutes).
- The call strike with the most OI is a resistance wall; the put strike with the most OI is a support wall.
- Levels within `LEVEL_CLUSTER_POINTS` (20 pts) of each other are merged. `strength` is how many sources agree.

**Bias:** a score from -4 to +4, one point each for:
- price vs the day's average price
- EMA 9 vs EMA 21 on 5-minute candles
- price vs CPR
- PCR (≥ 1.2 is bullish, ≤ 0.8 is bearish)

A score of +2 or more means BUY CE, -2 or less means BUY PE, and anything in between is NO TRADE.

The Nifty index has no volume, so the average price is TWAP rather than VWAP.

**Trade**
- **Entry:** the price breaking the last 3 completed candles' high (for a bullish trade) or low (for a bearish trade).
- **Stop-loss:** the nearest level behind the entry, at least 0.5 ATR away. If that's more than 3 ATR away, it uses 1.5 ATR instead.
- **Targets:** the next levels ahead of the entry.
- **No trade:** returned if the risk-reward to target 1 is below `MIN_RISK_REWARD`.

**Option:** the strike is chosen by `moneyness` from the live chain. Premiums at entry, stop-loss and targets are estimated as `LTP + delta × index move`. This ignores time decay and IV changes, so treat it as approximate.

## 5. Strategies

| Strategy | Buys CE when | Buys PE when | Stop-loss / targets | Works best |
|---|---|---|---|---|
| **Confluence** (default) | Score +2 or more (price vs day average, EMA 9/21, CPR, PCR) | Score -2 or less | Nearest support / resistance levels | Trending days |
| **Opening Range Breakout** | Price breaks above the 9:15–9:30 high | Price breaks below the 9:15–9:30 low | SL at the middle of the range; targets 1x and 2x the range | Strong first move, gap days |
| **EMA + VWAP Trend** | EMA 9 > EMA 21, price above the day average and EMA 9 | The opposite | Nearest support / resistance levels | Steady trends |
| **OI Wall Bounce** | Price just above the biggest put OI strike | Price just below the biggest call OI strike | SL beyond the wall; targets midway and at the other wall | Range-bound days, expiry |

Every strategy returns NO TRADE when its conditions aren't met, or when the risk-reward to target 1 is below `MIN_RISK_REWARD`.

## 6. Paper trading

On the page: pick a strategy, wait for a BUY signal, choose lots / entry / exit, and click **Paper trade this signal**.

The API equivalent is `POST /api/paper/trades` with:

```json
{"strategy": "confluence", "moneyness": "ATM", "expiry": "", "lots": 1,
 "order_type": "market", "exit_at": "T1"}
```

How it works:
- **Entry.** `order_type: "market"` buys now at the option's live ask. `"trigger"` waits until Nifty reaches the signal's entry level.
- **Exit.** A background check runs every `PAPER_MONITOR_SECONDS` (15 s). It closes the trade when Nifty hits the stop-loss or the chosen target (`exit_at`: `T1` or `T2`), filling at the live bid.
- **Auto square-off.** Open trades are closed at `PAPER_SQUARE_OFF` (15:15), and pending orders that never triggered are cancelled.
- **Charges.** An estimate of brokerage, STT, exchange fees, stamp duty and GST is deducted from each trade. The rates are set in `.env`; check them against your broker.
- **Storage.** Trades are kept in `paper_trades.db` (SQLite), so they survive restarts.
- **Results.** The page shows account value, open and booked P&L, win rate, a **performance by strategy** table and the trade history. This is how you see which strategy actually works before using real money.
- **Hours.** Paper trades can only be placed 9:15–15:15 IST, except in demo mode.

## 7. Auto paper trading (all strategies, whole day)

The engine starts with the server (`AUTO_TRADE_ENABLED=1`). Every trading day it:

1. **Before 9:15:** waits for the market to open.
2. **Needs today's Fyers login.** Fyers (per SEBI rules) needs a 2FA login every day. The page shows a **Log in to Fyers** button, and trading starts by itself once you've logged in.
3. **9:15–15:30:**
   - Every `AUTO_CYCLE_SECONDS` (30 s) it checks open trades for stop-loss or target.
   - Then it runs every strategy on live data.
   - When a strategy gives BUY CE or BUY PE, it opens a paper trade at the live ask price plus slippage.
4. **Limits:**
   - one open position per strategy at a time
   - at most `AUTO_MAX_TRADES_PER_STRATEGY` (6) trades per strategy per day
   - a `AUTO_COOLDOWN_MINUTES` (10) pause after each exit
   - new entries only between 09:20 and 14:45
5. **15:15:** squares off everything that's still open.
6. **After 15:30:** logs a day summary per strategy.

**Capital and position size.** ₹100 crore is split equally: ₹25 crore per strategy. Each trade takes the **smallest** of:
- **risk:** 0.5% of the strategy's capital (₹12.5 lakh) ÷ the option loss per lot if the stop-loss is hit
- **capital:** at most 10% of the strategy's capital (₹2.5 crore) spent on premium
- **liquidity:** at most 2% of the option's open interest, so a ₹100 crore account doesn't pretend to buy more than the market could realistically fill

The log line for each entry shows which of these limits decided the size.

**What you see on the page:**
- engine status (running, login needed, before open, entries closed, market closed) with a Start/Stop button
- a tile per strategy with its current signal, open position and live P&L, and today's trades, wins and P&L
- a **live log** of every signal change, entry, exit (with net P&L after charges), warning and the day summary, with filters
- the same log saved to `logs/auto_YYYY-MM-DD.log`

**Running it every morning automatically.** The server must be running before 9:15. Pick one:
- **Always-on machine or cloud VM (Linux):** run it as a service (`systemd`) or start it with `@reboot` in `crontab`, e.g. `@reboot cd /path/nifty_signal_api && python3 app.py >> logs/server.log 2>&1`
- **Windows:** Task Scheduler → "At log on" or daily at 09:00 → program `python`, arguments `app.py`, start in the project folder
- Then open the page each morning and click **Log in to Fyers** (once a day).

**Realism notes:**
- **Fills and slippage.** Fills use live bid/ask plus `AUTO_SLIPPAGE_PCT` (0.2%). Real orders of several crore would face more slippage, be split by the exchange's freeze-quantity limit, and are subject to position limits; none of that is modelled.
- **Rate limits.** One cycle uses about 4–5 Fyers data calls. That's about 10 per minute at the default 30 s, well inside the free plan's 50 per minute.
- **The process must stay running.** If the server is stopped during the day, open trades are checked again when it restarts, or closed at the last known price the next day.

## 8. Live market board (Nifty 50, order book, active options)

**Data source.** The server opens Fyers' **WebSocket data feed** after you log in and subscribes to the 50 stocks, Nifty, India VIX and any open paper-trade options. Every change is pushed live. It doesn't count against the 50-requests-per-minute limit, and the free plan allows 100 symbols. If the socket isn't delivering, the server falls back to regular quote requests every `FEED_REST_SECONDS` (10 s). The status badge shows **LIVE · WebSocket** or **Polling every 10 s**.

**What each part means:**

| Item | How it's worked out |
|---|---|
| Weight % | Free-float weight from `data/nifty50.json` (as of 16 Oct 2026), adjusted live for today's moves. Update the file monthly from the niftyindices.com factsheet, or when the index changes. |
| Nifty pts | Nifty's previous close × the stock's weight × its change % today: how many index points that stock added or removed. |
| Buy / sell side | Three checks, +1 or -1 each: up or down today; price above or below VWAP; pending buy quantity vs sell quantity in the order book (1.2× or more on one side). Score +3 is "Strong buying", -3 is "Strong selling". |
| Buy vs sell orders | Total pending buy quantity ÷ (buy + sell) from the live feed. Shown only in WebSocket mode. |
| Order book | Top 5 bid and ask levels with quantities and order counts, total buy and sell quantity, spread, VWAP and circuit limits. Refreshed every 3 s for the selected stock or option. |
| Most active options | From the live option chain: top options by volume, by OI added and by OI cut. |
| Option buildup | Price ↑ + OI ↑ long buildup · price ↓ + OI ↑ short buildup · price ↑ + OI ↓ short covering · price ↓ + OI ↓ long unwinding. |
| Trade at | Whether the last trade happened at the ask (buyers aggressive) or at the bid (sellers aggressive). |

**Caveats:**
- Pending order quantities can be placed and cancelled instantly, and large players hide size with iceberg orders, so read buy/sell pressure as a hint, not proof.
- The order-book and VWAP field names follow the Fyers SDK and docs but haven't been tested against a live account yet. If a box stays empty after you log in, check `logs/fyersApi.log`.

## 9. Fyers limits to know

- Standard (free) data API limits are 5 requests per second, 50 per minute and 5,000 per day.
- One `/signal` call uses up to 4 Fyers calls. Responses are cached for 2–300 seconds (see `.env.example`), so repeated calls inside the cache window cost nothing.
- Exceeding the per-minute limit more than 3 times in a day blocks you for the rest of that day. If you poll often, use Fyers Prime or increase the cache times.
- `NIFTY_LOT_SIZE` defaults to 65. Update it if NSE revises the lot size.

## 10. Tests

```bash
python -m pytest -q tests
```

The tests use a fake Fyers client that returns data in Fyers' documented response format. They cover:
- every strategy's rules
- bullish, bearish and no-trade signals
- paper trades hitting target 1, target 2 and the stop-loss
- trigger orders, cancelling, the 3:15 square-off, cash checks and charges
- login errors, the API key check and caching
- the auto engine: waiting for open and login, one position per strategy, sizing limits, cooldown, max trades, the entry cut-off, the day summary and the endpoints
- the market board: buy/sell side rules, live weights, index points, gainers and losers, option buildup, order-book parsing, WebSocket and polling modes, and paper exits using live feed prices without extra requests
