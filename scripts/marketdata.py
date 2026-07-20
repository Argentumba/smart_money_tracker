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

import os
import json
import gzip
import time
import datetime
import urllib.request
import urllib.error

# Браузерный UA — Stooq и Yahoo дружелюбнее к «браузерным» запросам, чем к
# кастомным/пустым UA (часть отказов с облачных IP именно из-за этого).
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Бесплатный ключ Twelve Data (секрет GitHub MARKETDATA_API_KEY). Если задан —
# это основной источник (стабилен с IP Actions); Stooq/Yahoo остаются фолбэком.
TWELVE_KEY = os.environ.get("MARKETDATA_API_KEY", "").strip()


def _raw(url, timeout=30, retries=3):
    """GET с браузерным UA и мягким ретраем на 429/5xx."""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/csv,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                enc = r.headers.get("Content-Encoding", "")
            time.sleep(0.2)
            if enc == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except Exception as e:  # таймауты, сетевые сбои
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise last


def _empty():
    return {"date": [], "open": [], "high": [], "low": [], "close": [], "volume": []}


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
        try:
            vol = float(p[5]) if len(p) > 5 and p[5] not in ("", "N/D") else 0.0
        except ValueError:
            vol = 0.0
        out["date"].append(p[0])
        out["open"].append(o); out["high"].append(h)
        out["low"].append(l); out["close"].append(c); out["volume"].append(vol)
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
    vols = q.get("volume") or []
    out = _empty()
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        o = opens[i] if i < len(opens) and opens[i] is not None else c
        h = highs[i] if i < len(highs) and highs[i] is not None else c
        l = lows[i] if i < len(lows) and lows[i] is not None else c
        v = vols[i] if i < len(vols) and vols[i] is not None else 0.0
        out["date"].append(datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"))
        out["open"].append(o); out["high"].append(h)
        out["low"].append(l); out["close"].append(c); out["volume"].append(v)
    return (out, None) if out["close"] else (None, "yahoo: пустой ряд")


def _td_parse(obj):
    """Разбор одного time_series из Twelve Data (values идут новыми→старыми)."""
    if not isinstance(obj, dict):
        return None, "td: неверный ответ"
    if obj.get("status") == "error" or "values" not in obj:
        return None, f"td: {str(obj.get('message', 'нет values'))[:60]}"
    out = _empty()
    for v in reversed(obj.get("values") or []):  # новые→старые ⇒ разворачиваем
        try:
            o, h = float(v["open"]), float(v["high"])
            l, c = float(v["low"]), float(v["close"])
        except (KeyError, ValueError, TypeError):
            continue
        try:
            vol = float(v.get("volume")) if v.get("volume") not in (None, "") else 0.0
        except (ValueError, TypeError):
            vol = 0.0
        out["date"].append(v.get("datetime", ""))
        out["open"].append(o); out["high"].append(h)
        out["low"].append(l); out["close"].append(c); out["volume"].append(vol)
    return (out, None) if out["close"] else (None, "td: пустой ряд")


def from_twelvedata_batch(stooq_symbols):
    """Один батч-запрос к Twelve Data по всем символам. → {stooq_symbol: (data|None, reason)}."""
    tickers = [s.split(".")[0].upper() for s in stooq_symbols]
    url = (f"https://api.twelvedata.com/time_series?symbol={','.join(tickers)}"
           f"&interval=1day&outputsize=300&apikey={TWELVE_KEY}")
    d = json.loads(_raw(url))
    res = {}
    if len(tickers) == 1:
        res[stooq_symbols[0]] = _td_parse(d)
        return res
    for stq, tk in zip(stooq_symbols, tickers):
        obj = d.get(tk)
        if obj is None:
            # при глобальной ошибке (напр. 429) плоский dict с message/status
            reason = (f"td: {str(d.get('message'))[:60]}"
                      if d.get("status") == "error" else f"td: нет {tk}")
            res[stq] = (None, reason)
        else:
            res[stq] = _td_parse(obj)
    return res


def daily(stooq_symbol):
    """Пробует keyless-источники по очереди. Возвращает (data|None, source|reason)."""
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


def daily_batch(stooq_symbols):
    """Грузит все символы. Twelve Data (если есть ключ) → фолбэк Stooq/Yahoo.

    Возвращает {stooq_symbol: (data|None, source|reason)}.
    """
    out = {}
    if TWELVE_KEY:
        try:
            td = from_twelvedata_batch(stooq_symbols)
        except Exception as e:
            td = {s: (None, f"td: {e}") for s in stooq_symbols}
        for s in stooq_symbols:
            data, reason = td.get(s, (None, "td: нет ответа"))
            if data:
                out[s] = (data, "twelvedata")
            else:
                fb, src = daily(s)  # фолбэк на keyless
                out[s] = (fb, src) if fb else (None, f"{reason}; {src}")
        return out
    # без ключа — только keyless
    for s in stooq_symbols:
        out[s] = daily(s)
    return out
