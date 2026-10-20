"""Persistent event cards for holdings (limit up/down + headlines).

Written when a holding hits limit move. Read back on later days so advice
does not forget 8/31-style events after the print recovers.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

JOURNAL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = JOURNAL_DIR / "runtime"
EVENTS_FILE = RUNTIME_DIR / "holding_events.json"
TZ_TAIPEI = timezone(timedelta(hours=8))
WINDOW_DAYS = 10
MAX_PER_SYMBOL = 8
RAID_TERMS = ("搜索", "檢調", "洗產地", "交保", "起訴", "內線", "假帳", "裁罰")


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TZ_TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_TAIPEI)
    return now.astimezone(TZ_TAIPEI)


def _norm_ticker(value) -> str:
    return str(value or "").strip().upper().replace(".TW", "").replace(".TWO", "")


def load_events() -> dict:
    if not EVENTS_FILE.is_file():
        return {}
    try:
        data = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_events(store: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def event_id(symbol: str, date_str: str, kind: str) -> str:
    return f"{_norm_ticker(symbol)}:{date_str}:{kind}"


def _kind_from_side(side: str | None) -> str | None:
    if side == "down":
        return "limit_down"
    if side == "up":
        return "limit_up"
    return None


def _label(kind: str) -> str:
    return "跌停" if kind == "limit_down" else ("漲停" if kind == "limit_up" else kind)


def _headlines(news_items) -> list[dict]:
    out = []
    for item in news_items or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title[:180],
                "url": str(item.get("url") or ""),
                "source": str(item.get("source") or ""),
            }
        )
        if len(out) >= 3:
            break
    return out


def raid_hint(headlines: list[dict]) -> str | None:
    blob = " ".join(row.get("title") or "" for row in headlines)
    for term in RAID_TERMS:
        if term in blob:
            return term
    return None


def upsert_event(store: dict, event: dict) -> bool:
    symbol = _norm_ticker(event.get("symbol"))
    if not symbol or not event.get("date") or not event.get("kind"):
        return False
    eid = event.get("id") or event_id(symbol, event["date"], event["kind"])
    event = dict(event)
    event["id"] = eid
    event["symbol"] = symbol
    rows = list(store.get(symbol) or [])
    existing = {row.get("id"): i for i, row in enumerate(rows) if isinstance(row, dict)}
    if eid in existing:
        old = rows[existing[eid]]
        merged = dict(old)
        if (event.get("headlines") or []) and len(event.get("headlines") or []) >= len(old.get("headlines") or []):
            merged["headlines"] = event["headlines"]
        if event.get("headline") and not old.get("headline"):
            merged["headline"] = event["headline"]
        if event.get("adviceThen") and not old.get("adviceThen"):
            merged["adviceThen"] = event["adviceThen"]
        for key in ("price", "dayChangePct", "name"):
            if event.get(key) not in (None, "") and old.get(key) in (None, ""):
                merged[key] = event[key]
        if merged == old:
            return False
        rows[existing[eid]] = merged
        store[symbol] = rows
        return True
    rows.append(event)
    rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    store[symbol] = rows[:MAX_PER_SYMBOL]
    return True


def backfill_from_news_cache(store: dict, cache: dict | None = None) -> int:
    if cache is None:
        try:
            from stock_news import load_cache

            cache = load_cache()
        except Exception:
            cache = {}
    added = 0
    for key, entry in (cache or {}).items():
        if not isinstance(entry, dict):
            continue
        parts = str(key).split(":")
        if len(parts) < 3:
            continue
        symbol, date_str, side = parts[0], parts[1], parts[2]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            continue
        kind = _kind_from_side(side)
        if not kind:
            continue
        headlines = _headlines(entry.get("items"))
        hint = raid_hint(headlines)
        event = {
            "id": event_id(symbol, date_str, kind),
            "symbol": symbol,
            "name": entry.get("name") or "",
            "date": date_str,
            "kind": kind,
            "label": _label(kind),
            "headline": (headlines[0]["title"] if headlines else _label(kind)),
            "headlines": headlines,
            "raidTerm": hint,
            "source": "limit_news_cache",
        }
        if upsert_event(store, event):
            added += 1
    return added


def quote_session_date(holding: dict, now: datetime | None = None) -> str:
    quote = holding.get("quote") if isinstance(holding.get("quote"), dict) else {}
    raw = str(quote.get("date") or "")
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    as_of = str(quote.get("asOf") or "")
    if re.match(r"\d{4}-\d{2}-\d{2}", as_of):
        return as_of[:10]
    return _now(now).strftime("%Y-%m-%d")


def record_from_holdings(payload: dict, store: dict, now: datetime | None = None) -> int:
    now = _now(now)
    added = 0
    for holding in payload.get("holdings") or []:
        news = holding.get("news") if isinstance(holding.get("news"), dict) else {}
        side = news.get("side")
        kind = _kind_from_side(side)
        if not kind:
            continue
        symbol = _norm_ticker(holding.get("symbol"))
        date_str = quote_session_date(holding, now)
        headlines = _headlines(news.get("items"))
        advice = holding.get("advice") if isinstance(holding.get("advice"), dict) else {}
        event = {
            "id": event_id(symbol, date_str, kind),
            "symbol": symbol,
            "name": holding.get("name") or "",
            "date": date_str,
            "kind": kind,
            "label": _label(kind),
            "headline": (headlines[0]["title"] if headlines else _label(kind)),
            "headlines": headlines,
            "raidTerm": raid_hint(headlines),
            "price": holding.get("price"),
            "dayChangePct": holding.get("dayChangePct"),
            "adviceThen": advice.get("action") or "",
            "source": "live_limit",
            "recordedAt": now.isoformat(timespec="seconds"),
        }
        if upsert_event(store, event):
            added += 1
    return added


def events_for_symbol(store: dict, symbol: str, now: datetime | None = None, window_days: int = WINDOW_DAYS) -> list[dict]:
    now = _now(now)
    cutoff = (now.date() - timedelta(days=window_days)).isoformat()
    rows = []
    for row in store.get(_norm_ticker(symbol)) or []:
        if not isinstance(row, dict):
            continue
        date_str = str(row.get("date") or "")
        if date_str < cutoff:
            continue
        rows.append(row)
    rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    return rows


def index_recent(store: dict, symbols: list[str], now: datetime | None = None) -> dict[str, list[dict]]:
    return {sym: events_for_symbol(store, sym, now=now) for sym in symbols if sym}


def public_event(row: dict) -> dict:
    return {
        "date": row.get("date"),
        "kind": row.get("kind"),
        "label": row.get("label") or _label(row.get("kind") or ""),
        "headline": row.get("headline") or "",
        "raidTerm": row.get("raidTerm"),
        "adviceThen": row.get("adviceThen") or "",
        "headlines": row.get("headlines") or [],
    }


def apply_event_scores(scores: dict, evidence: list, recent_events: list, today: str) -> None:
    """Mutate advice scores using events that are still inside the watch window."""
    if not recent_events:
        return
    down = [e for e in recent_events if e.get("kind") == "limit_down" and e.get("date") != today]
    up = [e for e in recent_events if e.get("kind") == "limit_up" and e.get("date") != today]
    if down:
        latest = down[0]
        date_label = str(latest.get("date") or "")[5:].replace("-", "/")
        hint = latest.get("raidTerm")
        bit = f"{date_label} 跌停事件觀察中"
        if hint:
            bit += f"（{hint}）"
        evidence.insert(0, bit)
        scores["dip"] = 0
        scores["add"] = 0
        scores["review"] += 30
        if hint:
            scores["review"] += 8
    if up:
        latest = up[0]
        date_label = str(latest.get("date") or "")[5:].replace("-", "/")
        evidence.insert(0 if not down else 1, f"{date_label} 漲停事件觀察中，不宜追高")
        scores["add"] = 0
        scores["avoid"] += 18
