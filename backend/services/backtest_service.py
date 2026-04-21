"""
Backtest engine using real market data from yfinance.

Prices are adjusted-close (auto_adjust=True), which accounts for:
  - Stock splits and reverse splits
  - Cash dividends (reinvested into price)
  - Spin-offs (best-effort via yfinance)

Weekly resampling: last trading price of each Friday.
Missing dates (IPO after start, delistings) are forward-filled then
backward-filled so gaps don't break the simulation.
"""
import asyncio
import math
import statistics
from datetime import date, datetime, timedelta

import pandas as pd
import yfinance as yf

from .sp500_data import SP500_CONSTITUENTS, SECTOR_ALTERNATIVES, INDEX_MAP

BENCHMARK = "SPY"
BATCH_SIZE = 100          # tickers per yfinance download call
MAX_SYMBOLS = 200         # cap to keep response times under ~30s


class BacktestService:

    async def _fetch_weekly_prices(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """
        Download adjusted-close for all symbols + benchmark, resample to weekly Friday closes.
        Runs batches concurrently in a thread pool so the async event loop isn't blocked.
        """
        all_syms = list(dict.fromkeys(symbols + [BENCHMARK]))  # deduplicate, preserve order
        batches = [all_syms[i : i + BATCH_SIZE] for i in range(0, len(all_syms), BATCH_SIZE)]
        loop = asyncio.get_event_loop()

        def _dl(batch: list[str]) -> pd.DataFrame:
            raw = yf.download(
                batch,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                return pd.DataFrame()
            if isinstance(raw.columns, pd.MultiIndex):
                return raw["Close"]
            # Single-ticker download returns flat columns
            return raw[["Close"]].rename(columns={"Close": batch[0]})

        frames = await asyncio.gather(
            *[loop.run_in_executor(None, _dl, b) for b in batches]
        )
        non_empty = [f for f in frames if not f.empty]
        if not non_empty:
            return pd.DataFrame()

        combined = pd.concat(non_empty, axis=1)
        # Remove duplicate columns (same symbol appeared in multiple batches)
        combined = combined.loc[:, ~combined.columns.duplicated()]
        # Resample to weekly Friday
        weekly = combined.resample("W-FRI").last()
        # Forward-fill (handles gaps mid-series), then backward-fill (handles missing start)
        return weekly.ffill().bfill()

    async def run_backtest(
        self,
        symbols: list[str],
        weights: dict[str, float],
        start_date: date,
        end_date: date,
        initial_investment: float = 100_000.0,
        simulate_tlh: bool = True,
        tax_rate: float = 0.20,
        tlh_threshold: float = 0.05,
        index: str = "sp500",
    ) -> dict:
        if end_date <= start_date:
            raise ValueError("end_date must be after start_date.")

        # Cap symbols by weight so the largest positions drive the simulation
        if len(symbols) > MAX_SYMBOLS:
            symbols = sorted(symbols, key=lambda s: weights.get(s, 0), reverse=True)[:MAX_SYMBOLS]

        try:
            prices = await self._fetch_weekly_prices(symbols, start_date, end_date)
        except Exception as exc:
            raise ValueError(f"Price data fetch failed: {exc}") from exc

        if prices.empty:
            raise ValueError("No price data returned — check date range and network access.")
        if BENCHMARK not in prices.columns:
            raise ValueError(f"Benchmark {BENCHMARK} data missing — check network access.")

        weeks = prices.index.tolist()
        if len(weeks) < 4:
            raise ValueError("Date range must span at least 4 weeks.")

        spy_series_raw = prices[BENCHMARK]
        sym_sector = {c["symbol"]: c["sector"] for c in SP500_CONSTITUENTS}

        # Symbols we actually have data for
        available = [s for s in symbols if s in prices.columns]
        total_w = sum(weights.get(s, 0) for s in available)
        if total_w <= 0:
            raise ValueError("No usable weights for available symbols.")

        # Initialise holdings at week-0 prices (real market prices, not normalised to 100)
        holdings: dict[str, dict] = {}
        for sym in available:
            w = weights.get(sym, 0) / total_w
            p0 = float(prices[sym].iloc[0])
            if p0 <= 0 or pd.isna(p0):
                continue
            shares = round(initial_investment * w / p0, 6)
            if shares >= 1e-6:
                holdings[sym] = {"shares": shares, "cost_basis": p0}

        spy_p0 = float(spy_series_raw.iloc[0])
        spy_shares = initial_investment / spy_p0 if spy_p0 > 0 else 0

        portfolio_values: list[dict] = []
        benchmark_values: list[dict] = []
        harvest_events: list[dict] = []
        total_tax_savings = 0.0
        # wash-sale: track timestamp of last harvest per symbol
        harvested_at: dict[str, datetime] = {}

        for i, week_ts in enumerate(weeks):
            dt_str = week_ts.strftime("%Y-%m-%d")
            week_dt = week_ts.to_pydatetime()

            # Current market prices for all held symbols
            cur: dict[str, float] = {}
            for sym, h in holdings.items():
                raw_p = prices[sym].iloc[i] if sym in prices.columns else float("nan")
                cur[sym] = float(raw_p) if not pd.isna(raw_p) else h["cost_basis"]

            # TLH sweep every 4 weeks
            if simulate_tlh and i > 0 and i % 4 == 0:
                for sym in list(holdings.keys()):
                    h = holdings[sym]
                    if h["shares"] < 1e-6 or h["cost_basis"] <= 0:
                        continue
                    last = harvested_at.get(sym)
                    if last and (week_dt - last).days < 30:
                        continue  # still in wash-sale window
                    price = cur.get(sym, h["cost_basis"])
                    loss_pct = (price - h["cost_basis"]) / h["cost_basis"]
                    if loss_pct > -tlh_threshold:
                        continue
                    loss_amt = (price - h["cost_basis"]) * h["shares"]
                    saving = abs(loss_amt) * tax_rate
                    total_tax_savings += saving
                    sector = sym_sector.get(sym)
                    replacement = self._find_replacement(sym, sector, holdings, cur, harvested_at, week_dt)
                    if replacement:
                        proceeds = price * h["shares"]
                        rep_price = cur[replacement]
                        new_shares = round(proceeds / rep_price, 6)
                        holdings[sym]["shares"] = 0.0
                        holdings[replacement]["shares"] += new_shares
                        holdings[replacement]["cost_basis"] = rep_price
                    else:
                        # Reset cost basis to current (step-up without selling)
                        holdings[sym]["cost_basis"] = price
                    harvested_at[sym] = week_dt
                    harvest_events.append({
                        "date": dt_str,
                        "symbol": sym,
                        "replacement": replacement,
                        "loss": round(loss_amt, 2),
                        "tax_saving": round(saving, 2),
                    })

            port_val = sum(
                h["shares"] * cur.get(sym, h["cost_basis"])
                for sym, h in holdings.items()
            )
            spy_val = spy_shares * float(spy_series_raw.iloc[i])
            portfolio_values.append({"date": dt_str, "value": round(port_val, 2)})
            benchmark_values.append({"date": dt_str, "value": round(spy_val, 2)})

        # Performance metrics
        p0, p1 = portfolio_values[0]["value"], portfolio_values[-1]["value"]
        b0, b1 = benchmark_values[0]["value"], benchmark_values[-1]["value"]
        years = max((end_date - start_date).days / 365.25, 0.01)

        port_total = (p1 - p0) / p0 * 100 if p0 > 0 else 0.0
        bench_total = (b1 - b0) / b0 * 100 if b0 > 0 else 0.0
        port_ann = ((p1 / p0) ** (1 / years) - 1) * 100 if p0 > 0 else 0.0
        bench_ann = ((b1 / b0) ** (1 / years) - 1) * 100 if b0 > 0 else 0.0

        weekly_rets = [
            (portfolio_values[j]["value"] - portfolio_values[j - 1]["value"])
            / portfolio_values[j - 1]["value"]
            for j in range(1, len(portfolio_values))
            if portfolio_values[j - 1]["value"] > 0
        ]
        rf_weekly = 0.045 / 52
        if len(weekly_rets) > 1:
            mean_r = statistics.mean(weekly_rets)
            std_r = statistics.stdev(weekly_rets)
            sharpe = (mean_r - rf_weekly) / std_r * math.sqrt(52) if std_r > 0 else 0.0
            volatility = std_r * math.sqrt(52) * 100
        else:
            sharpe = volatility = 0.0

        return {
            "portfolio_values": portfolio_values,
            "spy_values": benchmark_values,
            "data_source": "yfinance adjusted-close (dividends reinvested, splits adjusted)",
            "symbols_used": len([h for h in holdings.values() if h["shares"] >= 1e-6]),
            "metrics": {
                "portfolio_total_return": round(port_total, 2),
                "portfolio_annualized_return": round(port_ann, 2),
                "spy_total_return": round(bench_total, 2),
                "spy_annualized_return": round(bench_ann, 2),
                "alpha": round(port_ann - bench_ann, 2),
                "tax_alpha": round(total_tax_savings / initial_investment * 100, 2) if initial_investment > 0 else 0,
                "total_tax_savings": round(total_tax_savings, 2),
                "portfolio_max_drawdown": round(self._max_drawdown([v["value"] for v in portfolio_values]), 2),
                "spy_max_drawdown": round(self._max_drawdown([v["value"] for v in benchmark_values]), 2),
                "sharpe_ratio": round(sharpe, 2),
                "annualized_volatility": round(volatility, 2),
                "harvest_count": len(harvest_events),
                "years": round(years, 1),
            },
            "harvest_events": harvest_events[:50],
        }

    def _find_replacement(
        self,
        symbol: str,
        sector: str | None,
        holdings: dict,
        cur_prices: dict,
        harvested_at: dict,
        week_dt: datetime,
    ) -> str | None:
        for alt in SECTOR_ALTERNATIVES.get(sector or "", []):
            if alt == symbol:
                continue
            if alt not in holdings or holdings[alt]["shares"] < 1e-6:
                continue
            if alt not in cur_prices:
                continue
            last = harvested_at.get(alt)
            if last and (week_dt - last).days < 30:
                continue
            return alt
        return None

    def _max_drawdown(self, values: list[float]) -> float:
        peak = values[0] if values else 0.0
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak * 100
                if dd > max_dd:
                    max_dd = dd
        return max_dd


backtest_service = BacktestService()
