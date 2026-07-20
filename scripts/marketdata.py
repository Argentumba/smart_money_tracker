#!/usr/bin/env python3
"""
Загрузка дневных котировок из бесплатных источников без API-ключей.

Проблема: Stooq бесплатен, но с шаренных IP GitHub Actions часто отдаёт не CSV,
а текст «превышен дневной лимит» или режет по User-Agent → данные пустые.
Решение: браузерный User-Agent + фолбэк на второй источник (Yahoo Finance
chart API, тоже без ключа). Пробуем по очереди, берём первый рабочий.

Символ задаётся в формате Stooq (например 'bno.us', 'cf.us'); для Yahoo из него
выводится тикер ('BNO', 'CF'). Возвращаем нормализованный OHLC:
    {"date":[...], "open":[...], "high":[...], "low":[...], "close":[...]}  # старые→новые
или None и причину, если оба источника не дали данных.

Только стандартная библиотека.
"""

import json
import gzip
import time
import datetime
import urllib.request

# Браузерный UA — Stooq и Yahoo дружелюбнее к «браузерным» запросам, чем к
# кастомным/пустым UA (часть отказов с облачных IP именно из-за этого).
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _raw(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "text/csv,application/json,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        enc = r.headers.get("Content-Encoding", "")
    time.sleep(0.2)
    if enc == "gzip" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8", "ignore")


def _empty():
    return {"date": [], "open": [], "high": [], "low": [], "close": []}


def from_stooq(stooq_symbol):
    """Дневная история со Stooq (CSV). Возвращает (data|None, reason|None)."""
    txt = _raw(f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d")
    lines = [ln for ln in txt.strip().splitlines() if ln]
    if not lines or not lines[0].lower().startswith("date"):
        # тут обычно лежит «Exceeded the daily hits limit» и т.п.
        return None, f"stooq: {(lines[0][:60] if lines else 'пустой ответ')}"
    out = _empty()
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) < 5:
            continue
        try:
            o, h, l, c = float(p[1]), float(p[2]), float(p[3]), float(p[4])
        except ValueError:
            continue
        out["date"].append(p[0])
        out["open"].append(o); out["high"].append(h)
        out["low"].append(l); out["close"].append(c)
    return (out, None) if out["close"] else (None, "stooq: нет строк")


def from_yahoo(stooq_symbol, rng="1y"):
    """Дневная история из Yahoo Finance chart API (JSON, без ключа)."""
    tk = stooq_symbol.split(".")[0].upper()
    txt = _raw(f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}"
               f"?interval=1d&range={rng}")
    d = json.loads(txt)
    chart = d.get("chart") or {}
    if chart.get("error"):
        return None, f"yahoo: {chart['error']}"
    res = (chart.get("result") or [None])[0]
    if not res:
        return None, "yahoo: нет result"
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    opens = q.get("open") or []
    highs = q.get("high") or []
    lows = q.get("low") or []
    out = _empty()
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        o = opens[i] if i < len(opens) and opens[i] is not None else c
        h = highs[i] if i < len(highs) and highs[i] is not None else c
        l = lows[i] if i < len(lows) and lows[i] is not None else c
        out["date"].append(datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"))
        out["open"].append(o); out["high"].append(h)
        out["low"].append(l); out["close"].append(c)
    return (out, None) if out["close"] else (None, "yahoo: пустой ряд")


def daily(stooq_symbol):
    """Пробует источники по очереди. Возвращает (data|None, source|reason)."""
    last = "нет источников"
    for fn in (from_stooq, from_yahoo):
        try:
            data, reason = fn(stooq_symbol)
            if data:
                return data, fn.__name__.replace("from_", "")
            last = reason
        except Exception as e:
            last = f"{fn.__name__}: {e}"
    return None, last
