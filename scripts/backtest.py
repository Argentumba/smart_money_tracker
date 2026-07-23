#!/usr/bin/env python3
"""
БЭКТЕСТ СИГНАЛОВ (событийное исследование, event study).

Честный вопрос: реально ли сигнал даёт edge, или это иллюзия? Проходим по всей
истории цен, находим КАЖДЫЙ исторический срабатыш сигнала (той же логикой, что
на дашборде) и меряем форвард-доходность через 5/20/60 торговых дней.

Ключ к честности — БАЗОВАЯ ЛИНИЯ: та же форвард-доходность по ВСЕМ барам
(безусловный вход). Edge = доходность_сигнала − базовая. Если сигнал не бьёт
базу, у него нет edge, как бы красиво он ни выглядел.

Сигналы:
  • spring_up   — разжатие «пружины» вверх (пробой после сжатия волатильности);
  • spring_down — вниз;
  • above_200ma / below_200ma — трендовый режим (как санити-чек конвейера).

Данные — Twelve Data (длинная история). Пишет docs/data/backtest.json.

Ограничения (честно, вынесены и в JSON): выборка невелика, тест ин-сэмпл, без
комиссий/проскальзывания, выжившие тикеры, и прошлое ≠ будущее. Это оценка
базовых ставок, НЕ гарантия и НЕ рекомендация.

Только стандартная библиотека.
"""

import json
import time
from datetime import datetime, timezone

from fetch_13f import OUT
import marketdata

# Twelve Data free: 8 кредитов/мин, символ в батче = кредит. Грузим чанками
# по ≤7 символов с паузой ~минуту, иначе 16 сразу → 429 → мусорный фолбэк.
TD_CHUNK = 7
TD_PAUSE = 62

# Универсум для статистики (US-тикеры с историей). Больше имён → больше событий.
UNIVERSE = ["CF", "NTR", "MOS", "IPI", "UAN", "SQM", "ICL",
            "CLSK", "HUT", "RIOT", "GNL", "HAL", "NEM", "PSTL",
            "SPY", "QQQ"]
HORIZONS = [5, 20, 60]
BBW_N, BBW_HIST, RANGE_N, SQ_PCTILE = 20, 120, 20, 20


def _bbw_series(c, n=BBW_N):
    out = [None] * len(c)
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        m = sum(w) / n
        if m:
            sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
            out[i] = 4 * sd / m * 100
    return out


def spring_signals(o, h, l, c):
    """Множества индексов, где пружина исторически разжималась вверх/вниз."""
    n = len(c)
    bbw = _bbw_series(c)
    up, down = set(), set()
    for i in range(BBW_HIST + BBW_N, n):
        window = [x for x in bbw[i - BBW_HIST:i] if x is not None]
        if len(window) < BBW_HIST // 2 or bbw[i] is None or bbw[i - 1] is None:
            continue
        thr = sorted(window)[max(0, int(len(window) * SQ_PCTILE / 100) - 1)]
        prior = [x for x in bbw[i - 5:i] if x is not None]
        if not prior or min(prior) > thr:
            continue
        if bbw[i] <= bbw[i - 1]:            # ширина должна расширяться
            continue
        hi = max(h[i - RANGE_N:i])
        lo = min(l[i - RANGE_N:i])
        if c[i] > hi:
            up.add(i)
        elif c[i] < lo:
            down.add(i)
    return up, down


def ma_regime(c, n=200):
    above, below = set(), set()
    for i in range(n, len(c)):
        ma = sum(c[i - n:i]) / n
        (above if c[i] > ma else below).add(i)
    return above, below


def fwd_returns(c, idxs, k):
    """Форвард-доходности через k баров для набора индексов."""
    return [c[i + k] / c[i] - 1 for i in idxs if i + k < len(c) and c[i]]


def baseline(c, k):
    """Безусловная форвард-доходность по всем барам (базовая линия)."""
    return [c[i + k] / c[i] - 1 for i in range(len(c) - k) if c[i]]


def stats(rets):
    if not rets:
        return {"n": 0}
    rets = sorted(rets)
    n = len(rets)
    avg = sum(rets) / n
    med = rets[n // 2]
    hit = sum(1 for r in rets if r > 0) / n
    return {"n": n, "avg": round(avg * 100, 2), "median": round(med * 100, 2),
            "hit": round(hit * 100, 1)}


def fetch_universe(symbols, outputsize=5000):
    """Грузим чанками ≤TD_CHUNK с паузой, чтобы не ловить лимит 8/мин."""
    result = {}
    for ci in range(0, len(symbols), TD_CHUNK):
        grp = symbols[ci:ci + TD_CHUNK]
        if ci > 0:
            print(f"  … пауза {TD_PAUSE}с (лимит Twelve Data 8/мин)")
            time.sleep(TD_PAUSE)
        result.update(marketdata.daily_batch(grp, outputsize=outputsize))
    return result


def main():
    print("→ Бэктест сигналов (event study)")
    batch = fetch_universe([f"{t.lower()}.us" for t in UNIVERSE], outputsize=5000)

    # накапливаем форвард-доходности сигналов и базы по всему универсуму
    acc = {"spring_up": {k: [] for k in HORIZONS},
           "spring_down": {k: [] for k in HORIZONS},
           "above_200ma": {k: [] for k in HORIZONS},
           "below_200ma": {k: [] for k in HORIZONS}}
    base = {k: [] for k in HORIZONS}
    used, ev_up, ev_down = [], 0, 0

    need = BBW_HIST + BBW_N + max(HORIZONS) + 5
    for t in UNIVERSE:
        data, src = batch.get(f"{t.lower()}.us", (None, "нет"))
        nbars = len(data["close"]) if data else 0
        if nbars < need:
            print(f"  · {t}: пропуск ({nbars} баров, нужно {need}; источник {src})")
            continue
        print(f"  ✓ {t}: {nbars} баров [{src}]")
        o, h, l, c = data["open"], data["high"], data["low"], data["close"]
        up, down = spring_signals(o, h, l, c)
        above, below = ma_regime(c)
        ev_up += len(up); ev_down += len(down)
        used.append(t)
        for k in HORIZONS:
            acc["spring_up"][k] += fwd_returns(c, up, k)
            acc["spring_down"][k] += fwd_returns(c, down, k)
            acc["above_200ma"][k] += fwd_returns(c, above, k)
            acc["below_200ma"][k] += fwd_returns(c, below, k)
            base[k] += baseline(c, k)

    results = {}
    for sig, per_k in acc.items():
        results[sig] = {}
        for k in HORIZONS:
            s = stats(per_k[k])
            b = stats(base[k])
            if s.get("n") and b.get("n"):
                s["base_avg"] = b["avg"]
                s["edge"] = round(s["avg"] - b["avg"], 2)   # чистый edge над базой
                s["low_confidence"] = s["n"] < 30           # мало событий — не доверять
            results[sig][str(k)] = s

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "universe": used,
        "horizons": HORIZONS,
        "events": {"spring_up": ev_up, "spring_down": ev_down},
        "results": results,
        "notes": [
            "Edge = средняя доходность сигнала минус безусловная (базовая) — "
            "положительный edge = сигнал бьёт случайный вход.",
            "Ин-сэмпл, без комиссий/проскальзывания, выжившие тикеры, малая выборка.",
            "Инсайдеры/13F здесь НЕ бэктестятся (нет архива исторических подач).",
            "Оценка базовых ставок, не гарантия и не рекомендация.",
        ],
    }
    if not used:
        path = OUT / "backtest.json"
        if path.exists():
            print("→ Нет данных — оставляю прежний backtest.json.")
            return
    (OUT / "backtest.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"✓ Бэктест: {len(used)} тикеров, событий пружины ↑{ev_up}/↓{ev_down}")
    for sig in ("spring_up", "spring_down"):
        row = results.get(sig, {})
        cells = " · ".join(
            f"{k}д: {row[k].get('avg','—')}% (edge {row[k].get('edge','—')}, "
            f"hit {row[k].get('hit','—')}%, n={row[k].get('n',0)})"
            for k in map(str, HORIZONS) if k in row)
        print(f"  {sig}: {cells}")


if __name__ == "__main__":
    main()
