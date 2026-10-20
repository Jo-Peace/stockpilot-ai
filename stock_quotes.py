"""Live TW quotes via TWSE MIS, with Yahoo Finance fallback and a disk snapshot."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

JOURNAL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = JOURNAL_DIR / "runtime"
SNAPSHOT_FILE = RUNTIME_DIR / "quotes_snapshot.json"
HISTORY_FILE = RUNTIME_DIR / "history_6m.json"
TZ_TAIPEI = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CACHE_TTL_SEC = 20
HISTORY_TTL_SEC = 4 * 3600
TWSE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
TWSE_DAY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TPEX_DAY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _now_iso() -> str:
    return datetime.now(TZ_TAIPEI).isoformat(timespec="seconds")


def is_tw_symbol(symbol: str) -> bool:
    s = str(symbol or "").strip().upper()
    return bool(s) and s.isalnum() and s[0].isdigit() and len(s) <= 6


def yahoo_symbol(symbol: str, exchange: str | None = None) -> str:
    s = str(symbol or "").strip().upper()
    if not s:
        return s
    if "." in s:
        return s
    if not is_tw_symbol(s):
        return s
    if (exchange or "").lower() == "otc":
        return f"{s}.TWO"
    return f"{s}.TW"


def to_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def first_book_price(raw: str | None) -> float | None:
    if not raw:
        return None
    for part in str(raw).split("_"):
        price = to_float(part)
        if price and price > 0:
            return price
    return None


def parse_twse_row(row: dict) -> dict | None:
    symbol = str(row.get("c") or "").strip().upper()
    if not symbol:
        return None
    last = to_float(row.get("z"))
    bid = first_book_price(row.get("b"))
    ask = first_book_price(row.get("a"))
    prev = to_float(row.get("y"))
    price = last or bid or ask or prev
    if not price:
        return None
    change = (price - prev) if prev else None
    change_pct = (change / prev) if prev and change is not None else None
    tlong = to_float(row.get("tlong"))
    as_of = None
    if tlong:
        as_of = datetime.fromtimestamp(tlong / 1000, TZ_TAIPEI).isoformat(timespec="seconds")
    exchange = str(row.get("ex") or "tse").lower()
    return {
        "symbol": symbol,
        "name": str(row.get("n") or "").strip(),
        "price": price,
        "last": last,
        "bid": bid,
        "ask": ask,
        "prevClose": prev,
        "open": to_float(row.get("o")),
        "high": to_float(row.get("h")),
        "low": to_float(row.get("l")),
        "volumeLots": to_float(row.get("v")),
        "change": change,
        "changePct": change_pct,
        "time": str(row.get("t") or "").strip() or None,
        "date": str(row.get("d") or "").strip() or None,
        "exchange": exchange,
        "yahooSymbol": yahoo_symbol(symbol, exchange),
        "source": "twse",
        "asOf": as_of or _now_iso(),
    }


def parse_twse_payload(payload: dict) -> dict[str, dict]:
    quotes = {}
    for row in payload.get("msgArray") or []:
        if not isinstance(row, dict):
            continue
        parsed = parse_twse_row(row)
        if not parsed:
            continue
        existing = quotes.get(parsed["symbol"])
        if existing and (existing.get("volumeLots") or 0) >= (parsed.get("volumeLots") or 0):
            continue
        quotes[parsed["symbol"]] = parsed
    return quotes


def roc_date_to_iso(value: str) -> str | None:
    text = str(value or "").strip().replace("-", "/")
    parts = [p for p in text.split("/") if p]
    if len(parts) != 3:
        return None
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if year < 1911:
        year += 1911
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_tw_daily_rows(rows) -> list[tuple[str, float]]:
    points: list[tuple[str, float]] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        day = roc_date_to_iso(row[0])
        price = to_float(row[6])
        if not day or not price or price <= 0:
            continue
        points.append((day, round(price, 4)))
    return points


def _history_from_points(symbol: str, points: list[tuple[str, float]], source: str, yahoo_sym: str | None = None) -> dict | None:
    if len(points) < 2:
        return None
    uniq: dict[str, float] = {}
    for day, price in points:
        uniq[day] = price
    ordered = sorted(uniq.items())
    if len(ordered) < 2:
        return None
    closes = [item[1] for item in ordered]
    start, end = closes[0], closes[-1]
    change = end - start
    out = {
        "symbol": str(symbol or "").strip().upper(),
        "closes": closes,
        "start": start,
        "end": end,
        "high": max(closes),
        "low": min(closes),
        "change": change,
        "changePct": (change / start) if start else None,
        "startDate": ordered[0][0],
        "endDate": ordered[-1][0],
        "count": len(closes),
        "source": source,
        "fetchedAt": _now_iso(),
    }
    if yahoo_sym:
        out["yahooSymbol"] = yahoo_sym
    return out


def parse_yahoo_history(payload: dict, requested: str) -> dict | None:
    """Daily closes from a Yahoo chart payload (range=6mo, interval=1d)."""
    results = ((payload.get("chart") or {}).get("result")) or []
    if not results:
        return None
    row = results[0]
    timestamps = row.get("timestamp") or []
    quote = ((row.get("indicators") or {}).get("quote") or [{}])[0] or {}
    adj = ((row.get("indicators") or {}).get("adjclose") or [{}])[0] or {}
    closes_raw = quote.get("close") or adj.get("adjclose") or []
    points: list[tuple[str, float]] = []
    for ts, close in zip(timestamps, closes_raw):
        price = to_float(close)
        if not isinstance(ts, (int, float)) or not price or price <= 0:
            continue
        day = datetime.fromtimestamp(int(ts), TZ_TAIPEI).strftime("%Y-%m-%d")
        points.append((day, round(price, 4)))
    meta = row.get("meta") or {}
    symbol = requested.split(".")[0].upper()
    return _history_from_points(symbol, points, "yahoo", str(meta.get("symbol") or requested))


def parse_yahoo_chart(payload: dict, requested: str) -> dict | None:
    results = ((payload.get("chart") or {}).get("result")) or []
    if not results:
        return None
    meta = results[0].get("meta") or {}
    price = to_float(meta.get("regularMarketPrice"))
    prev = to_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
    if not price:
        return None
    change = (price - prev) if prev else None
    change_pct = (change / prev) if prev and change is not None else None
    market_time = meta.get("regularMarketTime")
    as_of = None
    if isinstance(market_time, (int, float)):
        as_of = datetime.fromtimestamp(market_time, TZ_TAIPEI).isoformat(timespec="seconds")
    symbol = requested.split(".")[0].upper()
    exchange = "otc" if str(meta.get("exchangeName") or "").upper() in {"TWO", "ROCO"} else "tse"
    return {
        "symbol": symbol,
        "name": str(meta.get("shortName") or meta.get("longName") or "").strip(),
        "price": price,
        "last": price,
        "bid": None,
        "ask": None,
        "prevClose": prev,
        "open": to_float(meta.get("regularMarketOpen")),
        "high": to_float(meta.get("regularMarketDayHigh")),
        "low": to_float(meta.get("regularMarketDayLow")),
        "volumeLots": None,
        "change": change,
        "changePct": change_pct,
        "time": None,
        "date": None,
        "exchange": exchange if is_tw_symbol(symbol) else str(meta.get("exchangeName") or "").lower(),
        "yahooSymbol": str(meta.get("symbol") or requested),
        "source": "yahoo",
        "asOf": as_of or _now_iso(),
    }


def _http_json(url: str, timeout: float = 8.0) -> dict:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://mis.twse.com.tw/stock/fibest.jsp",
    })
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        raise ValueError(f"non-json response from {url}")
    return json.loads(text[start:])


def fetch_twse(symbols: list[str]) -> dict[str, dict]:
    tw_symbols = [s.upper() for s in symbols if is_tw_symbol(s)]
    if not tw_symbols:
        return {}
    pairs = []
    for symbol in tw_symbols:
        lower = symbol.lower()
        pairs.append(f"tse_{lower}.tw")
        pairs.append(f"otc_{lower}.tw")
    query = urlencode({"ex_ch": "|".join(pairs), "json": "1", "delay": "0"})
    payload = _http_json(f"{TWSE_URL}?{query}", timeout=8.0)
    return parse_twse_payload(payload)


def fetch_yahoo_one(symbol: str, exchange: str | None = None) -> dict | None:
    ysym = yahoo_symbol(symbol, exchange)
    url = YAHOO_CHART.format(symbol=quote(ysym, safe=".")) + "?interval=1d&range=5d"
    payload = _http_json(url, timeout=8.0)
    parsed = parse_yahoo_chart(payload, ysym)
    return parsed


def fetch_yahoo_history(symbol: str, exchange: str | None = None, yahoo_sym: str | None = None) -> dict | None:
    wanted = str(symbol or "").strip().upper()
    if not wanted:
        return None
    candidates: list[str] = []
    if yahoo_sym:
        candidates.append(str(yahoo_sym).strip())
    primary = yahoo_symbol(wanted, exchange)
    if primary not in candidates:
        candidates.append(primary)
    if is_tw_symbol(wanted):
        alt = yahoo_symbol(wanted, "otc" if (exchange or "").lower() != "otc" else "tse")
        if alt not in candidates:
            candidates.append(alt)
    last_error = None
    for ysym in candidates:
        try:
            url = YAHOO_CHART.format(symbol=quote(ysym, safe=".")) + "?interval=1d&range=6mo"
            payload = _http_json(url, timeout=8.0)
            parsed = parse_yahoo_history(payload, ysym)
            if parsed:
                parsed["symbol"] = wanted
                return parsed
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return None


def month_starts(count: int = 6) -> list[datetime]:
    now = datetime.now(TZ_TAIPEI)
    year, month = now.year, now.month
    out: list[datetime] = []
    for _ in range(count):
        out.append(datetime(year, month, 1, tzinfo=TZ_TAIPEI))
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return out


def fetch_twse_history_month(symbol: str, month_dt: datetime) -> list[tuple[str, float]]:
    query = urlencode({
        "date": month_dt.strftime("%Y%m01"),
        "stockNo": symbol,
        "response": "json",
    })
    payload = _http_json(f"{TWSE_DAY_URL}?{query}", timeout=8.0)
    if str(payload.get("stat") or "") != "OK":
        return []
    return parse_tw_daily_rows(payload.get("data") or [])


def fetch_tpex_history_month(symbol: str, month_dt: datetime) -> list[tuple[str, float]]:
    query = urlencode({
        "date": month_dt.strftime("%Y/%m/01"),
        "code": symbol,
        "response": "json",
    })
    payload = _http_json(f"{TPEX_DAY_URL}?{query}", timeout=8.0)
    tables = payload.get("tables") or []
    rows = tables[0].get("data") if tables and isinstance(tables[0], dict) else []
    return parse_tw_daily_rows(rows)


def fetch_official_history(symbol: str, exchange: str | None = None) -> dict | None:
    wanted = str(symbol or "").strip().upper()
    if not wanted or not is_tw_symbol(wanted):
        return None
    prefer_otc = str(exchange or "").lower() in {"otc", "two", "roco"}
    order = (
        (fetch_tpex_history_month, fetch_twse_history_month)
        if prefer_otc
        else (fetch_twse_history_month, fetch_tpex_history_month)
    )
    points: list[tuple[str, float]] = []
    sources: list[str] = []
    for month_dt in month_starts(7):
        month_points: list[tuple[str, float]] = []
        for fetch_month in order:
            try:
                month_points = fetch_month(wanted, month_dt)
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                month_points = []
            if month_points:
                label = "tpex" if fetch_month is fetch_tpex_history_month else "twse"
                if label not in sources:
                    sources.append(label)
                break
        points.extend(month_points)
    source = "+".join(sources) if sources else "twse"
    return _history_from_points(wanted, points, source)


def fetch_one_history(symbol: str, exchange: str | None = None, yahoo_sym: str | None = None) -> dict | None:
    try:
        parsed = fetch_yahoo_history(symbol, exchange=exchange, yahoo_sym=yahoo_sym)
        if parsed:
            return parsed
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        parsed = None
    if is_tw_symbol(symbol):
        return fetch_official_history(symbol, exchange=exchange)
    return parsed


def load_history_cache() -> dict[str, dict]:
    if not HISTORY_FILE.is_file():
        return {}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, dict):
        return {}
    return {str(k).upper(): v for k, v in series.items() if isinstance(v, dict)}


def save_history_cache(series: dict[str, dict]) -> None:
    if not series:
        return
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(
        json.dumps({"savedAt": _now_iso(), "series": series}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _history_fresh(entry: dict | None) -> bool:
    if not entry or len(entry.get("closes") or []) < 2:
        return False
    cached_at = entry.get("cachedAt")
    if isinstance(cached_at, (int, float)):
        return (time.time() - cached_at) <= HISTORY_TTL_SEC
    fetched = str(entry.get("fetchedAt") or "")
    if not fetched:
        return False
    try:
        dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_TAIPEI)
    age = (datetime.now(TZ_TAIPEI) - dt.astimezone(TZ_TAIPEI)).total_seconds()
    return age <= HISTORY_TTL_SEC


def _public_history(entry: dict) -> dict:
    keys = (
        "symbol", "yahooSymbol", "closes", "start", "end", "high", "low",
        "change", "changePct", "startDate", "endDate", "count", "source", "fetchedAt",
    )
    return {k: entry[k] for k in keys if k in entry}


def fetch_histories(symbols: list[str], quotes: dict | None = None, fetch_one=None) -> dict:
    """6-month daily closes for holdings. Disk-cached so live quote polls stay cheap."""
    wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
    wanted = list(dict.fromkeys(wanted))
    quotes = quotes or {}
    fetch_one = fetch_one or fetch_one_history
    if not wanted:
        return {"series": {}, "errors": [], "missing": [], "cached": 0, "fetchedAt": _now_iso()}

    cache = load_history_cache()
    series: dict[str, dict] = {}
    stale: list[str] = []
    errors: list[str] = []

    for symbol in wanted:
        entry = cache.get(symbol)
        if _history_fresh(entry):
            series[symbol] = entry
        else:
            if entry and len(entry.get("closes") or []) >= 2:
                series[symbol] = entry
            stale.append(symbol)

    if stale:
        def _one(symbol: str) -> tuple[str, dict | None, str | None]:
            quote = quotes.get(symbol) or {}
            exchange = quote.get("exchange")
            ysym = quote.get("yahooSymbol")
            try:
                parsed = fetch_one(symbol, exchange=exchange, yahoo_sym=ysym)
                return symbol, parsed, None
            except TypeError:
                try:
                    parsed = fetch_one(symbol, exchange)
                    return symbol, parsed, None
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError, TypeError) as exc:
                    return symbol, None, f"yahoo-history {symbol}: {exc}"
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                return symbol, None, f"yahoo-history {symbol}: {exc}"

        workers = min(4, len(stale))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, symbol) for symbol in stale]
            for fut in as_completed(futures):
                symbol, parsed, error = fut.result()
                if error:
                    errors.append(error)
                if parsed and len(parsed.get("closes") or []) >= 2:
                    parsed = dict(parsed)
                    parsed["cachedAt"] = time.time()
                    series[symbol] = parsed
                    cache[symbol] = parsed
        try:
            save_history_cache(cache)
        except OSError:
            pass

    public = {sym: _public_history(series[sym]) for sym in wanted if sym in series}
    return {
        "series": public,
        "errors": errors,
        "missing": [s for s in wanted if s not in public],
        "cached": sum(1 for s in wanted if s in series and s not in stale),
        "fetchedAt": _now_iso(),
    }


def load_snapshot() -> dict[str, dict]:
    if not SNAPSHOT_FILE.is_file():
        return {}
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    quotes = data.get("quotes") if isinstance(data, dict) else None
    if not isinstance(quotes, dict):
        return {}
    return {str(k).upper(): v for k, v in quotes.items() if isinstance(v, dict)}


def save_snapshot(quotes: dict[str, dict]) -> None:
    if not quotes:
        return
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(
        json.dumps({"savedAt": _now_iso(), "quotes": quotes}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _from_cache(symbols: list[str]) -> dict[str, dict]:
    now = time.monotonic()
    found = {}
    with _cache_lock:
        for symbol in symbols:
            hit = _cache.get(symbol)
            if not hit:
                continue
            ts, quote = hit
            if now - ts <= CACHE_TTL_SEC:
                found[symbol] = quote
    return found


def _store_cache(quotes: dict[str, dict]) -> None:
    now = time.monotonic()
    with _cache_lock:
        for symbol, quote in quotes.items():
            _cache[symbol] = (now, quote)


def fetch_quotes(symbols: list[str], live: bool = True) -> dict:
    wanted = [str(s).strip().upper() for s in symbols if str(s).strip()]
    wanted = list(dict.fromkeys(wanted))
    quotes: dict[str, dict] = {}
    errors: list[str] = []
    sources = []

    if not wanted:
        return {"quotes": {}, "errors": [], "sources": [], "fetchedAt": _now_iso()}

    cached = _from_cache(wanted)
    quotes.update(cached)
    missing = [s for s in wanted if s not in quotes]

    if live and missing:
        try:
            twse = fetch_twse(missing)
            quotes.update(twse)
            if twse:
                sources.append("twse")
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"twse: {exc}")
        missing = [s for s in wanted if s not in quotes]

        for symbol in list(missing):
            try:
                parsed = fetch_yahoo_one(symbol)
                if not parsed and is_tw_symbol(symbol):
                    parsed = fetch_yahoo_one(symbol, exchange="otc")
                if parsed:
                    quotes[symbol] = parsed
                    if "yahoo" not in sources:
                        sources.append("yahoo")
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"yahoo {symbol}: {exc}")
        missing = [s for s in wanted if s not in quotes]

    if missing:
        snap = load_snapshot()
        for symbol in missing:
            if symbol in snap:
                row = dict(snap[symbol])
                row["source"] = f"snapshot:{row.get('source') or 'unknown'}"
                quotes[symbol] = row
        if any(str(quotes.get(s, {}).get("source") or "").startswith("snapshot") for s in wanted):
            sources.append("snapshot")

    live_rows = {k: v for k, v in quotes.items() if not str(v.get("source") or "").startswith("snapshot")}
    if live_rows:
        _store_cache(live_rows)
        merged_snap = load_snapshot()
        merged_snap.update(live_rows)
        save_snapshot(merged_snap)

    derived = []
    for quote in quotes.values():
        src = str(quote.get("source") or "")
        if src.startswith("snapshot"):
            label = "snapshot"
        else:
            label = src.split(":")[0]
        if label and label not in derived:
            derived.append(label)
    for extra in sources:
        if extra not in derived:
            derived.append(extra)

    return {
        "quotes": quotes,
        "errors": errors,
        "sources": derived,
        "fetchedAt": _now_iso(),
        "missing": [s for s in wanted if s not in quotes],
    }
