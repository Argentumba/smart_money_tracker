#!/usr/bin/env python3
"""
smart-money-monitor v2 — циклы и «сжатие пружины» по производителям удобрений.

Две оси анализа из дневных OHLC (Stooq, бесплатно, без ключей):

1. ЦИКЛ — где бумага в своём ценовом цикле:
   • положение в 52-недельном диапазоне (percentile);
   • цена относительно 200-дневной средней (долгий тренд);
   • импульс за 120 дней;
   → квадрант фазы: Спад / Восстановление / Экспансия / Пик.

2. СЖАТИЕ ПРУЖИНЫ (volatility compression / coiled spring):
   • ширина полос Боллинджера (BBW = 4σ/SMA20) в нижнем перцентиле своей
     истории = волатильность сжата, энергия копится → «пружина взведена»;
   • когда BBW начинает расширяться И цена пробивает 20-дневный диапазон —
     «пружина разжалась» (выстрел) вверх или вниз. Это и есть сигнал.

Честно: это технические эвристики, а не предсказание. Они говорят «здесь
накопилось напряжение и вот оно разрешилось», а не «дальше будет так-то».

Только стандартная библиотека. Сеть из некоторых датацентров закрыта —
при недоступности источника скрипт не падает и не затирает прошлый файл.
"""

import json
from datetime import datetime, timezone

from fetch_13f import get, OUT
import notify

# Производители удобрений (US-листинг, тикеры Stooq .us):
# азот — CF, UAN; калий — MOS, NTR, IPI, ICL; спец/литий-калий — SQM.
PRODUCERS = [
    {"ticker": "CF",  "name": "CF Industries (азот)",  "symbol": "cf.us"},
    {"ticker": "NTR", "name": "Nutrien (диверс.)",     "symbol": "ntr.us"},
    {"ticker": "MOS", "name": "Mosaic (калий/фосфат)", "symbol": "mos.us"},
    {"ticker": "IPI", "name": "Intrepid Potash",       "symbol": "ipi.us"},
    {"ticker": "UAN", "name": "CVR Partners (азот)",   "symbol": "uan.us"},
    {"ticker": "SQM", "name": "SQM (калий/литий)",     "symbol": "sqm.us"},
    {"ticker": "ICL", "name": "ICL Group",             "symbol": "icl.us"},
]

SQUEEZE_PCTILE = 20    # BBW в нижних 20% истории = сжатие
BBW_WINDOW = 20        # период полос Боллинджера
BBW_HIST = 120         # окно для перцентиля ширины
RANGE_N = 20           # диапазон пробоя


def parse_ohlc(csv_text):
    """CSV Stooq (Date,Open,High,Low,Close,Volume) → dict of lists, старые→новые."""
    dates, o, h, l, c = [], [], [], [], []
    lines = [ln for ln in csv_text.strip().splitlines() if ln]
    if not lines or not lines[0].lower().startswith("date"):
        return None
    for ln in lines[1:]:
        p = ln.split(",")
        if len(p) < 5:
            continue
        try:
            oo, hh, ll, cc = float(p[1]), float(p[2]), float(p[3]), float(p[4])
        except ValueError:
            continue
        dates.append(p[0]); o.append(oo); h.append(hh); l.append(ll); c.append(cc)
    return {"date": dates, "open": o, "high": h, "low": l, "close": c}


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def stdev(xs, n):
    w = xs[-n:]
    m = sum(w) / len(w)
    return (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5


def bbw_at(closes, end, n=BBW_WINDOW):
    """Ширина полос Боллинджера (% от средней) на срезе closes[:end]."""
    w = closes[end - n:end]
    m = sum(w) / n
    if m == 0:
        return 0.0
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return 4 * sd / m * 100  # (верх−низ)=4σ, нормируем на среднюю


def bbw_series(closes, n=BBW_WINDOW):
    return [bbw_at(closes, e, n) for e in range(n, len(closes) + 1)]


def pctile_rank(window, value):
    """Перцентиль value среди window (0..100)."""
    if not window:
        return None
    below = sum(1 for x in window if x <= value)
    return round(below / len(window) * 100)


def atr(highs, lows, closes, n=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < n:
        return None
    return sum(trs[-n:]) / n


def cycle_phase(above_200, mom_up):
    if above_200 and mom_up:
        return "Экспансия"
    if above_200 and not mom_up:
        return "Пик / замедление"
    if not above_200 and mom_up:
        return "Восстановление"
    return "Спад"


def analyze(data):
    c, h, l = data["close"], data["high"], data["low"]
    n = len(c)
    if n < BBW_WINDOW + 5:
        return None
    last = c[-1]

    # ── Цикл ──
    look52 = min(252, n)
    win = c[-look52:]
    hi52, lo52 = max(win), min(win)
    pct_52w = round((last - lo52) / (hi52 - lo52) * 100) if hi52 > lo52 else None
    ma50 = sma(c, 50) if n >= 50 else None
    ma200 = sma(c, 200) if n >= 200 else sma(c, n)
    above_200 = last > ma200 if ma200 else None
    look_mom = min(120, n - 1)
    mom_120 = round((last - c[-1 - look_mom]) / c[-1 - look_mom] * 100, 1) if c[-1 - look_mom] else None
    mom_up = (mom_120 or 0) > 0
    phase = cycle_phase(bool(above_200), mom_up)

    # ── Пружина ──
    series = bbw_series(c)
    bbw_now = series[-1]
    hist = series[-BBW_HIST:]
    bbw_pctile = pctile_rank(hist, bbw_now)
    # порог 20-го перцентиля окна
    thr = sorted(hist)[max(0, int(len(hist) * SQUEEZE_PCTILE / 100) - 1)]
    squeeze_now = bbw_now <= thr
    # была ли пружина сжата в последние 6 баров
    prior = series[-6:-1] if len(series) >= 6 else series[:-1]
    squeeze_recent = bool(prior) and min(prior) <= thr
    # пробой 20-дневного диапазона (исключая сегодня)
    hi_r = max(h[-RANGE_N - 1:-1]) if n > RANGE_N else max(h[:-1])
    lo_r = min(l[-RANGE_N - 1:-1]) if n > RANGE_N else min(l[:-1])
    expanding = len(series) >= 2 and bbw_now > series[-2]
    fired_up = squeeze_recent and expanding and last > hi_r
    fired_down = squeeze_recent and expanding and last < lo_r
    fired = fired_up or fired_down

    if fired_up:
        status, coil = "разжалась ↑", "вверх"
    elif fired_down:
        status, coil = "разжалась ↓", "вниз"
    elif squeeze_now:
        # направление взведённой пружины — по положению в диапазоне
        pos = pct_52w if pct_52w is not None else 50
        coil = "вверх" if pos >= 60 else ("вниз" if pos <= 40 else "нейтр.")
        status = "взведена"
    else:
        status, coil = "расслаблена", "—"

    a = atr(h, l, c)
    return {
        "price": round(last, 2),
        "date": data["date"][-1],
        "cycle": {
            "phase": phase,
            "pct_52w": pct_52w,
            "above_200": above_200,
            "mom_120": mom_120,
            "atr_pct": round(a / last * 100, 1) if a and last else None,
        },
        "spring": {
            "bbw_pctile": bbw_pctile,
            "squeeze": bool(squeeze_now),
            "fired": bool(fired),
            "status": status,
            "coil_dir": coil,
        },
    }


def build_alert(name, tk, r):
    e = notify.esc
    up = r["spring"]["coil_dir"] == "вверх"
    head = "🧨🟢 <b>ПРУЖИНА РАЗЖАЛАСЬ ↑</b>" if up else "🧨🔴 <b>ПРУЖИНА РАЗЖАЛАСЬ ↓</b>"
    cyc = r["cycle"]
    mom = cyc["mom_120"]
    return (f"{head} · ${e(tk)} {e(name)}\n"
            f"Сжатие волатильности разрешилось пробоем диапазона.\n"
            f"Цикл: <b>{e(cyc['phase'])}</b> · импульс "
            f"{'+' if (mom or 0) > 0 else ''}{mom}% за 120д · "
            f"в 52-нед. диапазоне {cyc['pct_52w']}%\n"
            f"Цена {r['price']}\n"
            f"<i>Технический сигнал сжатия/разжатия, не прогноз.</i>")


def main():
    print("→ v2: циклы и сжатие пружины по производителям удобрений")
    results = {}
    alerts = []
    for p in PRODUCERS:
        try:
            csv_text = get(f"https://stooq.com/q/d/l/?s={p['symbol']}&i=d")
        except Exception as ex:
            print(f"  ✗ {p['ticker']}: источник недоступен ({ex})")
            continue
        data = parse_ohlc(csv_text)
        if not data:
            print(f"  ✗ {p['ticker']}: нет данных")
            continue
        r = analyze(data)
        if not r:
            print(f"  ✗ {p['ticker']}: мало истории")
            continue
        r["name"] = p["name"]
        results[p["ticker"]] = r
        sp = r["spring"]
        print(f"  ✓ {p['ticker']}: {r['cycle']['phase']} · пружина {sp['status']} "
              f"(BBW {sp['bbw_pctile']}%)")
        if sp["fired"]:
            alerts.append((p["name"], p["ticker"], r))

    # Сводка по сектору
    phases = {}
    squeezed = 0
    for r in results.values():
        phases[r["cycle"]["phase"]] = phases.get(r["cycle"]["phase"], 0) + 1
        if r["spring"]["squeeze"]:
            squeezed += 1

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "producers": results,
        "summary": {"phases": phases, "squeezed": squeezed, "total": len(results)},
    }
    path = OUT / "fertilizers.json"
    if not results and path.exists():
        print("→ Ничего не собрано — оставляю прежний fertilizers.json.")
    else:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ Сохранено в {path} ({len(results)} эмитентов)")

    for name, tk, r in alerts:
        notify.send(build_alert(name, tk, r))
    print(f"→ Разжатий пружины (алертов): {len(alerts)}" if alerts
          else "→ Разжатий пружины нет — Telegram молчит.")


if __name__ == "__main__":
    main()
