"""Holding-level action notes from live quotes + latest daily structured_data.

This is a personal research overlay, not licensed investment advice.
It does not call an LLM: the daily report already is the analysis product.
Refresh stays fast enough for the 45s quote loop.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_TAIPEI = timezone(timedelta(hours=8))
JOURNAL_DIR = Path(__file__).resolve().parent
WORKSPACE = JOURNAL_DIR.parent.parent
DAILY_DIR = WORKSPACE / "stock-youtube" / "smart_financial_report" / "reports" / "daily"
STOCK_DB_PATH = WORKSPACE / "stock-youtube" / "smart_financial_report" / "data" / "stock_db.json"
TICKER_RE = re.compile(r"(?<![A-Z0-9])(\d{4,6}[A-Z]?)(?![A-Z0-9])", re.I)
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DISCLAIMER = "本機研究筆記：即時報價 + 最新盤後分析，給自己看，不是投顧建議。"

ACTIONS = {
    "trim": {
        "key": "trim", "label": "減碼防守", "tone": "trim", "urgency": 0,
        "meaning": "這才是考慮賣：分批減碼，不是清倉指令。",
    },
    "review": {
        "key": "review", "label": "檢視部位", "tone": "review", "urgency": 1,
        "meaning": "先核對還要不要留，不是立刻砍。",
    },
    "avoid": {
        "key": "avoid", "label": "避開追高", "tone": "avoid", "urgency": 2,
        "meaning": "不要再買進；現有部位先抱，不是叫你賣掉。",
    },
    "lock": {
        "key": "lock", "label": "續抱不追", "tone": "lock", "urgency": 3,
        "meaning": "帳面已大賺：部位先抱，不要加碼追價。",
    },
    "dip": {
        "key": "dip", "label": "回補觀察", "tone": "dip", "urgency": 4,
        "meaning": "回檔可觀察要不要接，不是必買。",
    },
    "add": {
        "key": "add", "label": "加碼觀察", "tone": "add", "urgency": 5,
        "meaning": "可觀察加碼，不是立刻買。",
    },
    "hold": {
        "key": "hold", "label": "續抱", "tone": "hold", "urgency": 6,
        "meaning": "維持現況，先不要動。",
    },
    "watch": {
        "key": "watch", "label": "盤中觀察", "tone": "watch", "urgency": 7,
        "meaning": "方向不明，先看、先不要下單。",
    },
}


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TZ_TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_TAIPEI)
    return now.astimezone(TZ_TAIPEI)


def _now_iso(now: datetime | None = None) -> str:
    return _now(now).isoformat(timespec="seconds")


def _num(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm_ticker(value) -> str:
    text = str(value or "").strip().upper().replace(".TW", "").replace(".TWO", "")
    return text


def _clip(text, n=160) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def load_latest_report(daily_dir: Path | None = None) -> tuple[dict | None, Path | None]:
    root = daily_dir or DAILY_DIR
    if not root.is_dir():
        return None, None
    dated = []
    for child in root.iterdir():
        if not child.is_dir() or not DATE_DIR_RE.match(child.name):
            continue
        path = child / "structured_data.json"
        if path.is_file():
            dated.append((child.name, path))
    if not dated:
        return None, None
    dated.sort(key=lambda item: item[0])
    path = dated[-1][1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, path
    if not isinstance(data, dict):
        return None, path
    return data, path


def _name_map(holdings: list[dict]) -> dict[str, str]:
    mapping = {}
    if STOCK_DB_PATH.is_file():
        try:
            db = json.loads(STOCK_DB_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            db = {}
        if isinstance(db, dict):
            for ticker, info in db.items():
                name = str((info or {}).get("name") or "").strip()
                if name:
                    mapping[name] = _norm_ticker(ticker)
    for row in holdings:
        symbol = _norm_ticker(row.get("symbol"))
        name = str(row.get("name") or "").strip()
        if symbol and name:
            mapping[name] = symbol
    return mapping


def resolve_symbols(text: str, name_to_symbol: dict[str, str]) -> set[str]:
    found = set()
    if not text:
        return found
    for match in TICKER_RE.finditer(text):
        found.add(_norm_ticker(match.group(1)))
    names = sorted(name_to_symbol, key=len, reverse=True)
    for name in names:
        if name and name in text:
            found.add(name_to_symbol[name])
    return {s for s in found if s}


def _blank_mention() -> dict:
    return {
        "tags": [],
        "snips": [],
        "stop": None,
        "direction": None,
        "reason": None,
        "invalidation": None,
    }


def _tag(row: dict, tag: str, snip=None, **extra) -> None:
    if tag not in row["tags"]:
        row["tags"].append(tag)
    if snip:
        clipped = _clip(snip)
        if clipped and clipped not in row["snips"]:
            row["snips"].append(clipped)
    for key, value in extra.items():
        if value in (None, ""):
            continue
        if not row.get(key):
            row[key] = value


def index_report(report: dict | None, holdings: list[dict]) -> dict[str, dict]:
    holdings = holdings or []
    by_symbol = {_norm_ticker(h.get("symbol")): _blank_mention() for h in holdings if h.get("symbol")}
    if not report or not by_symbol:
        return by_symbol
    names = _name_map(holdings)

    def mention(text, tag, **extra):
        for symbol in resolve_symbols(str(text or ""), names):
            row = by_symbol.get(symbol)
            if row is not None:
                _tag(row, tag, text, **extra)

    for item in report.get("prediction_targets") or []:
        if not isinstance(item, dict):
            continue
        symbol = _norm_ticker(item.get("id") or item.get("ticker"))
        row = by_symbol.get(symbol)
        if row is None:
            continue
        direction = str(item.get("direction") or "")
        tag = "prediction_bull" if "空" not in direction else "prediction_bear"
        _tag(
            row,
            tag,
            item.get("trigger") or item.get("reason"),
            direction=direction or None,
            reason=item.get("trigger") or item.get("reason"),
            invalidation=item.get("stop_loss_desc"),
            stop=_num(item.get("stop_loss_price")),
        )

    for item in report.get("short_term_picks") or []:
        if not isinstance(item, dict):
            continue
        symbol = _norm_ticker(item.get("ticker") or item.get("id"))
        row = by_symbol.get(symbol)
        if row is None:
            continue
        direction = str(item.get("direction") or "")
        tag = "short_bull" if "空" not in direction else "short_bear"
        _tag(
            row,
            tag,
            item.get("reason"),
            direction=direction or row.get("direction"),
            reason=item.get("reason") or row.get("reason"),
            invalidation=item.get("invalidation") or row.get("invalidation"),
        )

    for item in report.get("long_term_picks") or []:
        if not isinstance(item, dict):
            continue
        blob = " ".join(str(item.get(k) or "") for k in ("theme", "ticker", "name", "thesis", "evidence"))
        for symbol in resolve_symbols(blob, names):
            row = by_symbol.get(symbol)
            if row is not None:
                _tag(row, "long_bull", item.get("thesis") or item.get("theme"))

    for text in report.get("safe_havens_bought") or []:
        mention(text, "foreign_buy")
        mention(text, "safe_haven")
    for text in report.get("heavyweights_dumped") or []:
        mention(text, "foreign_sell")
    for text in report.get("trust_conflicts") or []:
        mention(text, "conflict")
    for text in report.get("price_volume_divergence") or []:
        mention(text, "divergence")
    for text in (report.get("conservative_strategy"), report.get("aggressive_strategy")):
        mention(text, "strategy")
    return by_symbol


def _range_pos(holding: dict) -> float | None:
    quote = holding.get("quote") or {}
    price = _num(holding.get("price"))
    high = _num(quote.get("high"))
    low = _num(quote.get("low"))
    if price is None or high is None or low is None or high <= low:
        return None
    return (price - low) / (high - low)


def _quote_stamp(holding: dict) -> str | None:
    quote = holding.get("quote") or {}
    if quote.get("asOf"):
        return str(quote["asOf"])
    date = str(quote.get("date") or "")
    time = str(quote.get("time") or "")
    if len(date) == 8 and time:
        return f"{date[0:4]}-{date[4:6]}-{date[6:8]}T{time}+08:00"
    return None


def twse_session(now: datetime | None = None) -> dict:
    """Regular TWSE cash session: weekdays 09:00–13:30 Taipei. Holidays not listed."""
    now = _now(now)
    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute
    is_weekday = weekday < 5
    open_m = 9 * 60
    close_m = 13 * 60 + 32
    is_open = is_weekday and open_m <= minutes < close_m
    if not is_weekday:
        label = "週休"
    elif minutes < open_m:
        label = "開盤前"
    elif is_open:
        label = "盤中"
    else:
        label = "已收盤"
    return {
        "open": is_open,
        "label": label,
        "asOf": now.isoformat(timespec="seconds"),
        "closeLabel": now.replace(hour=13, minute=30, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _label_from_iso(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).replace("T", " ").replace("+08:00", "").strip()[:19]


def _session_label(payload: dict) -> str | None:
    stamps = []
    for holding in payload.get("holdings") or []:
        stamp = _quote_stamp(holding)
        if stamp:
            stamps.append(stamp)
    meta = (payload.get("quotesMeta") or {}).get("fetchedAt")
    if stamps:
        return max(stamps)
    return meta


def _sold_today_qty(payload: dict, now: datetime | None) -> dict[str, float]:
    today = _now(now).strftime("%Y-%m-%d")
    qty: dict[str, float] = {}
    for row in payload.get("realized") or []:
        if str(row.get("date") or "") != today:
            continue
        symbol = _norm_ticker(row.get("symbol"))
        if not symbol:
            continue
        qty[symbol] = qty.get(symbol, 0.0) + (_num(row.get("qty"), 0.0) or 0.0)
    return qty


def decide_action(
    holding: dict,
    mention: dict,
    concentration: float,
    sold_today: float = 0.0,
    recent_events: list | None = None,
    as_of: str | None = None,
) -> dict:
    mention = mention or _blank_mention()
    tags = set(mention.get("tags") or [])
    ret = _num(holding.get("ret"), 0.0) or 0.0
    day = _num(holding.get("dayChangePct"))
    price = _num(holding.get("price"))
    stop = _num(mention.get("stop"))
    pos = _range_pos(holding)
    evidence = []
    scores = {key: 0 for key in ACTIONS}

    bullish = bool(tags & {"prediction_bull", "short_bull", "long_bull", "foreign_buy", "safe_haven"})
    bearish_flow = "foreign_sell" in tags or "prediction_bear" in tags or "short_bear" in tags
    unmarked = not bullish and not bearish_flow

    if abs(ret) >= 0.05:
        evidence.append(f"未實現 {ret:+.0%}")
    if sold_today > 0:
        lots = sold_today / 1000 if sold_today >= 1000 and abs(sold_today / 1000 - round(sold_today / 1000)) < 1e-6 else None
        evidence.append(f"今日已賣 {lots:g} 張" if lots else f"今日已賣 {sold_today:g} 股")
    if mention.get("reason"):
        evidence.append(_clip(mention["reason"], 80))
    if "foreign_buy" in tags:
        evidence.append("盤後外資買超")
    if "foreign_sell" in tags:
        evidence.append("盤後外資賣超")
    if "conflict" in tags:
        evidence.append("外資／投信方向衝突")
    if "divergence" in tags:
        evidence.append("量價背離")
    if concentration >= 0.22:
        evidence.append(f"佔持倉 {concentration:.0%}")
    if day is not None:
        evidence.append(f"今日 {day:+.2%}")
    if pos is not None and pos <= 0.18:
        evidence.append("贴近今日低點")
    elif pos is not None and pos >= 0.85:
        evidence.append("贴近今日高點")

    if day is not None and day <= -0.04 and ret >= 0.4:
        scores["trim"] += 45
        evidence.append("大幅獲利且今日大跌")
    if day is not None and day <= -0.025 and concentration >= 0.28:
        scores["trim"] += 22
        evidence.append("權值部位偏重且今日下跌")
    if bearish_flow and day is not None and day < 0:
        scores["trim"] += 20
    if stop and price and price < stop:
        scores["trim"] += 40
        scores["review"] += 10
        evidence.append(f"跌破盤後停損 {stop:g}")
    if ret >= 3.0 and day is not None and day <= -0.03:
        scores["trim"] += 18
        evidence.append("超額報酬開始回吐")

    if ret >= 0.8 and day is not None and day >= 0.025:
        scores["avoid"] += 28
        evidence.append("已大幅獲利，不宜追高")
    if ret >= 2.0 and pos is not None and pos >= 0.85 and (day or 0) >= 0:
        scores["avoid"] += 16

    if ret >= 1.0 and (day is None or day > -0.03):
        scores["lock"] += 26
        evidence.append("帳面已大賺：先抱著，不要加碼追價")
    elif ret >= 0.5 and unmarked and (day is None or abs(day) < 0.02):
        scores["lock"] += 14

    if ret <= -0.18 and not bullish:
        scores["review"] += 32
        evidence.append("盤後未點名，先核對要不要留")

    if bullish and day is not None and day <= -0.01:
        scores["dip"] += 34
    if bullish and (day is None or day < 0.025) and ret < 0.9 and concentration < 0.28:
        scores["add"] += 30
    if bullish:
        scores["hold"] += 22
    elif ret >= 0.05:
        scores["hold"] += 16
        if not any("盤後" in bit or "未實現" in bit for bit in evidence[:2]):
            evidence.append("盤後未點名，部位仍為正報酬")
    if sold_today > 0:
        scores["hold"] += 6
        scores["add"] = max(0, scores["add"] - 20)

    quote = holding.get("quote") or {}
    prev = _num(quote.get("prevClose"))
    limit_down = (day is not None and day <= -0.095) or (
        prev and price and prev > 0 and abs(price / prev - 0.9) <= 0.002
    )
    if limit_down:
        scores["dip"] = 0
        scores["add"] = 0
        scores["lock"] = 0
        scores["hold"] = 0
        scores["watch"] = 0
        scores["trim"] = 0
        scores["review"] += 50
        evidence.insert(0, "跌停，先當事件風險，不是回補")

    try:
        from stock_events import apply_event_scores

        apply_event_scores(scores, evidence, recent_events or [], as_of or "")
    except Exception:
        pass

    mixed = unmarked and abs(ret) < 0.08 and (day is None or abs(day) < 0.015) and not tags
    if mixed:
        scores["watch"] += 12
    elif unmarked and abs(ret) < 0.05 and not tags:
        scores["watch"] += 8

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], ACTIONS[kv[0]]["urgency"]))
    action_key = ranked[0][0] if ranked[0][1] > 0 else ("hold" if ret >= 0 else "watch")
    spec = ACTIONS[action_key]
    reason_bits = evidence[:4]
    if not reason_bits:
        reason_bits = ["盤後未點名，僅依即時報價與持倉損益"]
    return {
        "symbol": _norm_ticker(holding.get("symbol")),
        "name": holding.get("name") or "",
        "action": spec["label"],
        "actionKey": spec["key"],
        "tone": spec["tone"],
        "urgency": spec["urgency"],
        "meaning": spec.get("meaning") or "",
        "reason": "；".join(reason_bits),
        "evidence": evidence[:6],
        "tags": mention.get("tags") or [],
        "stop": stop,
        "invalidation": mention.get("invalidation"),
        "concentration": concentration,
        "quoteAsOf": _quote_stamp(holding),
        "events": [],
        "eventWatch": bool(recent_events),
    }


def build_advice(
    payload: dict,
    report: dict | None = None,
    now: datetime | None = None,
    events_by_symbol: dict | None = None,
) -> dict:
    now = _now(now)
    holdings = list(payload.get("holdings") or [])
    mentions = index_report(report, holdings)
    sum_mkt = _num(payload.get("sumMkt"), 0.0) or sum(_num(h.get("mkt"), 0.0) or 0.0 for h in holdings)
    sold_today = _sold_today_qty(payload, now)
    events_by_symbol = events_by_symbol or {}
    as_of = now.strftime("%Y-%m-%d")
    items = []
    for holding in holdings:
        mkt = _num(holding.get("mkt"), 0.0) or 0.0
        conc = (mkt / sum_mkt) if sum_mkt else 0.0
        symbol = _norm_ticker(holding.get("symbol"))
        recent_events = events_by_symbol.get(symbol) or []
        row = decide_action(
            holding,
            mentions.get(symbol),
            conc,
            sold_today=sold_today.get(symbol, 0.0),
            recent_events=recent_events,
            as_of=as_of,
        )
        if recent_events:
            try:
                from stock_events import public_event

                row["events"] = [public_event(event) for event in recent_events[:3]]
            except Exception:
                row["events"] = recent_events[:3]
        items.append(row)
    items.sort(key=lambda row: (row["urgency"], -(_num(row.get("concentration"), 0) or 0)))
    counts = Counter(row["action"] for row in items)
    headline_bits = [
        f"{label} {counts[label]}"
        for label, _urgency in sorted(
            ((spec["label"], spec["urgency"]) for spec in ACTIONS.values()),
            key=lambda item: item[1],
        )
        if counts[label]
    ]
    report_date = str((report or {}).get("date") or "") or None
    stale_days = None
    if report_date:
        try:
            stale_days = (now.date() - datetime.strptime(report_date, "%Y-%m-%d").date()).days
        except ValueError:
            stale_days = None
    quote_as_of = _session_label(payload)
    session = twse_session(now)
    generated_at = now.isoformat(timespec="seconds")
    generated_label = now.strftime("%Y-%m-%d %H:%M:%S")
    frozen = False
    if not session["open"]:
        frozen = True
        freeze_stamp = quote_as_of or now.replace(hour=13, minute=30, second=0, microsecond=0).isoformat(timespec="seconds")
        generated_at = freeze_stamp
        generated_label = _label_from_iso(freeze_stamp) or session["closeLabel"]
    return {
        "ok": True,
        "generatedAt": generated_at,
        "generatedAtLabel": generated_label,
        "timezone": "Asia/Taipei",
        "frozenToClose": frozen,
        "marketSession": session,
        "quoteAsOf": quote_as_of,
        "quotesFetchedAt": (payload.get("quotesMeta") or {}).get("fetchedAt"),
        "reportDate": report_date,
        "reportStaleDays": stale_days,
        "indexAction": (report or {}).get("index_action"),
        "conservative": (report or {}).get("conservative_strategy"),
        "aggressive": (report or {}).get("aggressive_strategy"),
        "headline": f"{len(items)} 檔：" + "、".join(headline_bits) if items else "目前沒有持股",
        "counts": dict(counts),
        "disclaimer": DISCLAIMER,
        "items": items,
        "hasReport": bool(report),
    }


def attach_advice(payload: dict, report: dict | None = None, now: datetime | None = None) -> dict:
    loaded_path = None
    if report is None:
        report, loaded_path = load_latest_report()
    events_store = {}
    events_by_symbol = {}
    symbols = [_norm_ticker(h.get("symbol")) for h in payload.get("holdings") or [] if h.get("symbol")]
    try:
        from stock_events import backfill_from_news_cache, index_recent, load_events, save_events

        events_store = load_events()
        if backfill_from_news_cache(events_store):
            save_events(events_store)
        events_by_symbol = index_recent(events_store, symbols, now=now)
    except Exception:
        events_store = {}
        events_by_symbol = {}
    try:
        pack = build_advice(payload, report=report, now=now, events_by_symbol=events_by_symbol)
        if loaded_path is not None:
            try:
                pack["reportPath"] = str(loaded_path.relative_to(WORKSPACE))
            except ValueError:
                pack["reportPath"] = str(loaded_path)
    except Exception as exc:
        pack = {
            "ok": False,
            "generatedAt": _now_iso(now),
            "generatedAtLabel": _now(now).strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Taipei",
            "disclaimer": DISCLAIMER,
            "error": str(exc),
            "items": [],
            "headline": "操作建議計算失敗",
        }
    payload["advice"] = pack
    by_symbol = {row.get("symbol"): row for row in pack.get("items") or []}
    for holding in payload.get("holdings") or []:
        holding["advice"] = by_symbol.get(_norm_ticker(holding.get("symbol")))
    try:
        from stock_news import attach_limit_news

        attach_limit_news(payload, now=now)
    except Exception:
        pass
    try:
        from stock_events import index_recent, public_event, record_from_holdings, save_events

        if record_from_holdings(payload, events_store, now=now):
            save_events(events_store)
        events_by_symbol = index_recent(events_store, symbols, now=now)
        for holding in payload.get("holdings") or []:
            sym = _norm_ticker(holding.get("symbol"))
            evs = [public_event(event) for event in events_by_symbol.get(sym) or []][:3]
            holding["events"] = evs
            if isinstance(holding.get("advice"), dict):
                holding["advice"]["events"] = evs
                holding["advice"]["eventWatch"] = bool(evs)
        for row in pack.get("items") or []:
            evs = [public_event(event) for event in events_by_symbol.get(row.get("symbol")) or []][:3]
            row["events"] = evs
            row["eventWatch"] = bool(evs)
        pack["holdingEvents"] = [
            {"symbol": sym, "count": len(events_by_symbol.get(sym) or [])}
            for sym in symbols
            if events_by_symbol.get(sym)
        ]
    except Exception:
        pass
    return payload
