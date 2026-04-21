import httpx
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Any
from ..config import settings

class FinnhubCache:
    def __init__(self):
        self._cache: dict[str, tuple[Any, datetime]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            data, ts = self._cache[key]
            if datetime.utcnow() - ts < timedelta(seconds=settings.CACHE_TTL):
                return data
        return None

    def set(self, key: str, value: Any):
        self._cache[key] = (value, datetime.utcnow())

_cache = FinnhubCache()

class FinnhubClient:
    def __init__(self):
        self._sem = asyncio.Semaphore(10)

    async def _get(self, endpoint: str, params: dict = None) -> dict:
        cache_key = f"{endpoint}:{params}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached
        params = dict(params or {})
        params["token"] = settings.FINNHUB_API_KEY
        async with self._sem:
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"{settings.FINNHUB_BASE_URL}{endpoint}",
                        params=params,
                        timeout=10.0
                    )
                    r.raise_for_status()
                    data = r.json()
                    _cache.set(cache_key, data)
                    return data
            except Exception:
                return {}

    async def get_quote(self, symbol: str) -> dict:
        data = await self._get("/quote", {"symbol": symbol})
        return {
            "symbol": symbol,
            "current_price": data.get("c", 0),
            "change": data.get("d", 0),
            "change_percent": data.get("dp", 0),
            "high": data.get("h", 0),
            "low": data.get("l", 0),
            "previous_close": data.get("pc", 0),
        }

    async def get_company_profile(self, symbol: str) -> dict:
        data = await self._get("/stock/profile2", {"symbol": symbol})
        return {
            "symbol": symbol,
            "name": data.get("name", ""),
            "sector": data.get("finnhubIndustry", ""),
            "market_cap": data.get("marketCapitalization", 0),
            "country": data.get("country", ""),
            "currency": data.get("currency", "USD"),
            "exchange": data.get("exchange", ""),
            "logo": data.get("logo", ""),
            "weburl": data.get("weburl", ""),
        }

    async def get_multiple_quotes(self, symbols: list[str]) -> dict[str, dict]:
        tasks = [self.get_quote(s) for s in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {s: r for s, r in zip(symbols, results) if not isinstance(r, Exception)}

    async def get_dividends(self, symbol: str, from_date: str, to_date: str) -> list[dict]:
        """
        Fetch dividend history for a symbol.

        Finnhub endpoint: GET /stock/dividend?symbol=X&from=YYYY-MM-DD&to=YYYY-MM-DD
        Returns a list of dicts with fields: symbol, date (ex-div), payDate, recordDate,
        amount, currency, adjustedAmount, declarationDate, freq.
        """
        data = await self._get("/stock/dividend", {
            "symbol": symbol,
            "from": from_date,
            "to": to_date,
        })
        if not isinstance(data, list):
            return []
        return data

    async def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> dict:
        cache_key = f"/stock/candle:{symbol}:{resolution}:{from_ts}:{to_ts}"
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "from": from_ts,
            "to": to_ts,
            "token": settings.FINNHUB_API_KEY,
        }
        async with self._sem:
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"{settings.FINNHUB_BASE_URL}/stock/candle",
                        params=params,
                        timeout=30.0,
                    )
                    r.raise_for_status()
                    data = r.json()
                    if data.get("s") == "ok":
                        _cache.set(cache_key, data)
                    return data
            except Exception as e:
                return {"s": "error", "error": str(e)}

finnhub_client = FinnhubClient()
