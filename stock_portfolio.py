"""Personal stock journal: FIFO holdings, realized P&L, on-disk journal."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

JOURNAL_DIR = Path(__file__).resolve().parent

def _resolve_seed_file() -> Path:
    """Seed ledger: $STOCK_SEED_FILE > seed.local.json (gitignored, private) > demo_seed.json."""
    env = os.environ.get("STOCK_SEED_FILE")
    if env:
        return Path(env).expanduser()
    local = JOURNAL_DIR / "seed.local.json"
    if local.is_file():
        return local
    return JOURNAL_DIR / "demo_seed.json"


SEED_FILE = _resolve_seed_file()
# Extra symbols fetched so 6M sparklines work for common watch names.
EXTRA_SPARKLINE_SYMBOLS = ["2330", "2308", "2312", "2383", "3037", "3260", "4958", "6182", "0050", "2454", "2317", "2382", "3293"]
RUNTIME_DIR = JOURNAL_DIR / "runtime"
RUNTIME_FILE = RUNTIME_DIR / "journal.json"
TZ_TAIPEI = timezone(timedelta(hours=8))


def journal_path() -> Path:
    if RUNTIME_FILE.is_file():
        return RUNTIME_FILE
    return SEED_FILE


def _now_iso() -> str:
    return datetime.now(TZ_TAIPEI).isoformat(timespec="seconds")


def load_journal(path: Path | None = None) -> dict:
    target = path or journal_path()
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("journal root must be an object")
    trades = data.get("trades")
    if not isinstance(trades, list):
        raise ValueError("journal missing trades[]")
    prices = data.get("prices") if isinstance(data.get("prices"), dict) else {}
    data["trades"] = trades
    data["prices"] = {str(k).upper(): v for k, v in prices.items()}
    data["_path"] = str(target.relative_to(JOURNAL_DIR.parent.parent) if JOURNAL_DIR.parent.parent in target.parents else target)
    data["_isRuntime"] = target == RUNTIME_FILE
    data["_seedFile"] = SEED_FILE.name
    return data


def save_journal(data: dict) -> Path:
    payload = {
        "version": int(data.get("version") or 1),
        "exportedAt": _now_iso(),
        "source": data.get("source") or "dashboard assistant",
        "trades": data.get("trades") or [],
        "prices": data.get("prices") or {},
    }
    if data.get("realizedSummary"):
        payload["realizedSummary"] = data["realizedSummary"]
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return RUNTIME_FILE


def reset_journal() -> dict:
    if RUNTIME_FILE.exists():
        RUNTIME_FILE.unlink()
    return load_journal(SEED_FILE)


def norm_symbol(value) -> str:
    return str(value or "").strip().upper()


def _num(value, default=0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_trade(raw: dict) -> dict:
    symbol = norm_symbol(raw.get("symbol"))
    side = str(raw.get("side") or raw.get("type") or "").strip().lower()
    action = str(raw.get("action") or "").strip()
    if not side and action:
        if action in ("買進", "買入", "buy"):
            side = "buy"
        elif action in ("賣出", "sell"):
            side = "sell"
    qty = _num(raw.get("qty"))
    price = _num(raw.get("price"))
    if side not in {"buy", "sell"}:
        raise ValueError("side 必須是 buy 或 sell")
    if not symbol:
        raise ValueError("缺少股票代號")
    if qty <= 0:
        raise ValueError("股數必須大於 0")
    if price < 0:
        raise ValueError("價格不可為負")
    date = str(raw.get("date") or "").strip() or datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    trade_id = str(raw.get("id") or "").strip() or f"dash-{datetime.now(TZ_TAIPEI).strftime('%Y%m%d%H%M%S')}-{symbol}"
    return {
        "id": trade_id,
        "date": date,
        "side": side,
        "market": str(raw.get("market") or "TW").strip().upper() or "TW",
        "symbol": symbol,
        "name": str(raw.get("name") or "").strip(),
        "qty": qty,
        "price": price,
        "fee": _num(raw.get("fee")),
        "tax": _num(raw.get("tax")),
        "note": str(raw.get("note") or "").strip(),
    }


def add_trade(raw: dict) -> dict:
    data = load_journal()
    trade = normalize_trade(raw)
    data["trades"] = list(data.get("trades") or [])
    data["trades"].append(trade)
    data.setdefault("prices", {})
    data["prices"][trade["symbol"]] = trade["price"]
    save_journal(data)
    return load_journal()


def replace_journal(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("trades"), list):
        raise ValueError("檔案格式不對：需要 trades[]")
    trades = [normalize_trade(t) for t in raw["trades"]]
    prices = raw.get("prices") if isinstance(raw.get("prices"), dict) else {}
    data = {
        "version": 1,
        "source": raw.get("source") or "dashboard import",
        "trades": trades,
        "prices": {norm_symbol(k): _num(v) for k, v in prices.items() if norm_symbol(k)},
        "realizedSummary": raw.get("realizedSummary"),
    }
    save_journal(data)
    return load_journal()


def compute_ledger(trades: list, prices: dict | None = None) -> dict:
    """FIFO lots + realized rows. prices[symbol] is the fallback mark if no live quote."""
    prices = {norm_symbol(k): _num(v, default=float("nan")) for k, v in (prices or {}).items()}
    sorted_trades = sorted(
        trades,
        key=lambda t: (str(t.get("date") or ""), str(t.get("id") or "")),
    )
    lots: dict[str, list[dict]] = {}
    realized: list[dict] = []
    last_meta: dict[str, dict] = {}

    for raw in sorted_trades:
        try:
            t = normalize_trade(raw)
        except ValueError:
            continue
        sym = t["symbol"]
        lots.setdefault(sym, [])
        last_meta[sym] = {"name": t["name"], "market": t["market"]}
        qty = t["qty"]
        price = t["price"]
        fee = t["fee"]
        tax = t["tax"]

        if t["side"] == "buy":
            lots[sym].append({"qty": qty, "cost": qty * price + fee})
            continue

        remain = qty
        matched_cost = 0.0
        matched_qty = 0.0
        while remain > 1e-12 and lots[sym]:
            lot = lots[sym][0]
            take = min(lot["qty"], remain)
            unit = lot["cost"] / lot["qty"] if lot["qty"] else 0.0
            matched_cost += unit * take
            matched_qty += take
            lot["qty"] -= take
            lot["cost"] -= unit * take
            remain -= take
            if lot["qty"] <= 1e-12:
                lots[sym].pop(0)
        if matched_qty > 0:
            ratio = matched_qty / qty
            net_proceeds = matched_qty * price - fee * ratio - tax * ratio
            pnl = net_proceeds - matched_cost
            realized.append({
                "date": t["date"],
                "symbol": sym,
                "name": t["name"],
                "qty": matched_qty,
                "costPrice": matched_cost / matched_qty,
                "sellPrice": price,
                "pnl": pnl,
                "cost": matched_cost,
                "ret": (pnl / matched_cost) if matched_cost else 0.0,
            })

    holdings = []
    for symbol, arr in lots.items():
        qty = sum(lot["qty"] for lot in arr)
        cost = sum(lot["cost"] for lot in arr)
        if qty <= 1e-9:
            continue
        avg = cost / qty
        fallback = prices.get(symbol)
        mark = fallback if fallback == fallback and fallback is not None and fallback > 0 else avg
        mkt = mark * qty
        unreal = mkt - cost
        meta = last_meta.get(symbol) or {}
        holdings.append({
            "symbol": symbol,
            "name": meta.get("name") or "",
            "market": meta.get("market") or "TW",
            "qty": qty,
            "cost": cost,
            "avg": avg,
            "fallbackPrice": mark,
            "mkt": mkt,
            "unreal": unreal,
            "ret": (unreal / cost) if cost else 0.0,
        })
    holdings.sort(key=lambda h: h["symbol"])
    realized.sort(key=lambda r: r["date"], reverse=True)
    return {
        "holdings": holdings,
        "realized": realized,
        "sumRealized": sum(r["pnl"] for r in realized),
        "sumUnreal": sum(h["unreal"] for h in holdings),
        "sumCost": sum(h["cost"] for h in holdings),
        "sumMkt": sum(h["mkt"] for h in holdings),
        "symbols": [h["symbol"] for h in holdings],
    }


def apply_quotes(ledger: dict, quotes: dict) -> dict:
    """Mark holdings to live quotes; keep fallback when a symbol has no quote."""
    out = deepcopy(ledger)
    holdings = []
    for h in out["holdings"]:
        row = dict(h)
        quote = quotes.get(h["symbol"]) or quotes.get(h["symbol"].lower()) or {}
        live = quote.get("price")
        row["quote"] = quote or None
        src = str(quote.get("source") or "")
        if isinstance(live, (int, float)) and live > 0:
            row["price"] = float(live)
            row["priceSource"] = "snapshot" if src.startswith("snapshot") else "live"
            row["quoteSource"] = src
        else:
            row["price"] = h["fallbackPrice"]
            row["priceSource"] = "fallback"
            row["quoteSource"] = src or "fallback"
        row["mkt"] = row["price"] * row["qty"]
        row["unreal"] = row["mkt"] - row["cost"]
        row["ret"] = (row["unreal"] / row["cost"]) if row["cost"] else 0.0
        row["dayChange"] = quote.get("change")
        row["dayChangePct"] = quote.get("changePct")
        holdings.append(row)
    out["holdings"] = holdings
    out["sumUnreal"] = sum(h["unreal"] for h in holdings)
    out["sumMkt"] = sum(h["mkt"] for h in holdings)
    out["quotesOk"] = sum(1 for h in holdings if h.get("priceSource") == "live")
    out["quotesTotal"] = len(holdings)
    return out


def apply_histories(payload: dict, series: dict | None = None) -> dict:
    """Attach 6-month daily closes to each holding. Optionally pin the last point to live price."""
    series = series or {}
    today = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    holdings = []
    for h in payload.get("holdings") or []:
        row = dict(h)
        hist = series.get(row.get("symbol")) or series.get(str(row.get("symbol") or "").upper())
        if not hist or len(hist.get("closes") or []) < 2:
            row["history6m"] = None
            holdings.append(row)
            continue
        closes = [float(c) for c in hist["closes"] if isinstance(c, (int, float))]
        if len(closes) < 2:
            row["history6m"] = None
            holdings.append(row)
            continue
        start = closes[0]
        end = closes[-1]
        end_date = hist.get("endDate")
        live = row.get("price")
        if isinstance(live, (int, float)) and live > 0 and end_date != today:
            closes = closes + [float(live)]
            end = float(live)
            end_date = today
        elif isinstance(live, (int, float)) and live > 0:
            closes = closes[:-1] + [float(live)]
            end = float(live)
        change = end - start
        change_pct = (change / start) if start else None
        row["history6m"] = {
            "closes": closes,
            "start": start,
            "end": end,
            "high": max(closes),
            "low": min(closes),
            "change": change,
            "changePct": change_pct,
            "startDate": hist.get("startDate"),
            "endDate": end_date,
            "count": len(closes),
            "source": hist.get("source") or "yahoo",
            "fetchedAt": hist.get("fetchedAt"),
        }
        holdings.append(row)
    payload["holdings"] = holdings
    return payload


def annotate_journal(payload: dict, journal: dict) -> dict:
    payload["ok"] = True
    payload["tradeCount"] = len(journal.get("trades") or [])
    payload["journalPath"] = journal.get("_path")
    payload["isRuntime"] = journal.get("_isRuntime")
    payload["seedFile"] = journal.get("_seedFile")
    payload["source"] = journal.get("source")
    payload["exportedAt"] = journal.get("exportedAt")
    payload["realizedSummary"] = journal.get("realizedSummary")
    return payload


def build_portfolio(live: bool = True) -> dict:
    from stock_quotes import fetch_histories, fetch_quotes

    journal = load_journal()
    ledger = compute_ledger(journal["trades"], journal.get("prices") or {})
    quote_result = fetch_quotes(ledger.get("symbols") or [], live=live)
    payload = apply_quotes(ledger, quote_result.get("quotes") or {})
    history_result = fetch_histories(
        ledger.get("symbols") or [],
        quotes=quote_result.get("quotes") or {},
    )
    apply_histories(payload, history_result.get("series") or {})
    annotate_journal(payload, journal)
    payload["quotesMeta"] = {
        "fetchedAt": quote_result.get("fetchedAt"),
        "sources": quote_result.get("sources"),
        "errors": quote_result.get("errors"),
        "missing": quote_result.get("missing"),
    }
    payload["historyMeta"] = {
        "fetchedAt": history_result.get("fetchedAt"),
        "cached": history_result.get("cached"),
        "errors": history_result.get("errors"),
        "missing": history_result.get("missing"),
    }
    from stock_advice import attach_advice

    attach_advice(payload)
    return payload
