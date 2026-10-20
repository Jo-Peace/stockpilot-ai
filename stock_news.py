"""Fetch a few headlines when a holding hits limit up/down.

Uses Google News RSS (no API key). Cached per symbol + session date so the
45s quote loop does not keep hitting the network. Never raises into portfolio.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

JOURNAL_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = JOURNAL_DIR / "runtime"
CACHE_FILE = RUNTIME_DIR / "limit_news.json"
TZ_TAIPEI = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CACHE_TTL_SEC = 3 * 60 * 60
MAX_ITEMS = 3
EVENT_TERMS = (
    "搜索", "檢調", "交保", "起訴", "搜索票", "洗產地", "調查",
    "重訊", "重大", "跌停", "漲停", "停工", "罰款", "裁罰", "虛偽",
    "內線", "假帳", "產地",
)

def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TZ_TAIPEI)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_TAIPEI)
    return now.astimezone(TZ_TAIPEI)


def _num(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def limit_side(holding: dict) -> str | None:
    day = _num(holding.get("dayChangePct"))
    price = _num(holding.get("price"))
    prev = _num((holding.get("quote") or {}).get("prevClose"))
    if day is not None and day <= -0.095:
        return "down"
    if day is not None and day >= 0.095:
        return "up"
    if prev and price and prev > 0:
        ratio = price / prev
        if abs(ratio - 0.9) <= 0.002:
            return "down"
        if abs(ratio - 1.1) <= 0.002:
            return "up"
    return None


def _cache_key(symbol: str, side: str, now: datetime) -> str:
    return f"{symbol}:{now.strftime('%Y-%m-%d')}:{side}"


def load_cache() -> dict:
    if not CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(cache: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cache_fresh(entry: dict, now: datetime) -> bool:
    if not entry or not isinstance(entry, dict):
        return False
    fetched = str(entry.get("fetchedAt") or "")
    try:
        ts = datetime.fromisoformat(fetched)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=TZ_TAIPEI)
    except ValueError:
        return False
    return (_now(now) - ts.astimezone(TZ_TAIPEI)).total_seconds() <= CACHE_TTL_SEC


def parse_rss(xml_text: str) -> list[dict]:
    if not xml_text or "<item" not in xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter("item"):
        title = unescape((node.findtext("title") or "").strip())
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        source = ""
        src_el = node.find("{http://purl.org/dc/elements/1.1/}creator") or node.find("source")
        if src_el is not None and src_el.text:
            source = src_el.text.strip()
        if not title:
            continue
        blob = title
        score = sum(4 for term in EVENT_TERMS if term in blob)
        items.append({
            "title": title[:180],
            "url": link,
            "published": pub,
            "source": source or "Google 新聞",
            "score": score,
        })
    items.sort(key=lambda row: (-row["score"], row["title"]))
    out = []
    seen = set()
    for row in items:
        key = re.sub(r"\s+", "", row["title"])[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append({k: row[k] for k in ("title", "url", "published", "source")})
        if len(out) >= MAX_ITEMS:
            break
    return out


def _http_text(url: str, timeout: float = 8.0) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml,text/xml,*/*"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_google_news(symbol: str, name: str, side: str) -> list[dict]:
    flag = "跌停" if side == "down" else "漲停"
    query = f"{symbol} {name} {flag}".strip()
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    return parse_rss(_http_text(url))


def attach_limit_news(payload: dict, now: datetime | None = None, fetch=None) -> dict:
    now = _now(now)
    fetch = fetch or fetch_google_news
    cache = load_cache()
    dirty = False
    for holding in payload.get("holdings") or []:
        symbol = str(holding.get("symbol") or "").strip().upper()
        name = str(holding.get("name") or "").strip()
        side = limit_side(holding)
        advice = holding.get("advice") if isinstance(holding.get("advice"), dict) else {}
        if not symbol or not side:
            if advice:
                advice.pop("news", None)
                advice.pop("limitSide", None)
            continue
        key = _cache_key(symbol, side, now)
        entry = cache.get(key)
        if not cache_fresh(entry, now):
            items = []
            error = None
            try:
                items = fetch(symbol, name, side)
            except Exception as exc:
                error = str(exc)
                items = (entry or {}).get("items") or []
            entry = {
                "fetchedAt": now.isoformat(timespec="seconds"),
                "symbol": symbol,
                "name": name,
                "side": side,
                "query": f"{symbol} {name} {'跌停' if side == 'down' else '漲停'}",
                "items": items,
                "error": error,
            }
            cache[key] = entry
            dirty = True
        news = {
            "side": side,
            "label": "跌停" if side == "down" else "漲停",
            "fetchedAt": entry.get("fetchedAt"),
            "items": entry.get("items") or [],
            "error": entry.get("error"),
        }
        holding["news"] = news
        if advice:
            advice["news"] = news
            advice["limitSide"] = side
        pack_items = (payload.get("advice") or {}).get("items") if isinstance(payload.get("advice"), dict) else None
        if isinstance(pack_items, list):
            for row in pack_items:
                if str(row.get("symbol") or "").upper() == symbol:
                    row["news"] = news
                    row["limitSide"] = side
    if dirty:
        try:
            save_cache(cache)
        except OSError:
            pass
    pack = payload.get("advice")
    if isinstance(pack, dict):
        flagged = []
        for holding in payload.get("holdings") or []:
            news = holding.get("news")
            if not news:
                continue
            flagged.append({
                "symbol": holding.get("symbol"),
                "name": holding.get("name"),
                "side": news.get("side"),
                "label": news.get("label"),
                "items": news.get("items") or [],
            })
        pack["limitNews"] = flagged
    return payload
