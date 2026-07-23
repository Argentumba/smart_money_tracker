#!/usr/bin/env python3
"""
СЛОЙ СИГНАЛОВ (отдельный, необязательный, легко удаляемый).

Не тянет данные из сети и НЕ трогает остальные скрипты. Только читает уже
готовые docs/data/*.json (13F, инсайдеры, удобрения v2, макро, циклы) и
синтезирует из них единый ранжированный список действенных сигналов +
общую «рыночную позу». Пишет docs/data/signals.json.

Крутить: правь WEIGHTS и пороги ниже. Удалить слой целиком: убери этот файл,
шаг в workflow и вкладку «Сигналы» в docs/index.html — на остальное не влияет.

НЕ инвест-рекомендация. Сигналы = «здесь совпало несколько независимых
факторов», а не указание к сделке.

Только стандартная библиотека.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fetch_13f import OUT, PORTFOLIO_NAMES

# ── КОНФИГ (крути свободно) ───────────────────────────────────
WEIGHTS = {
    "confluence":       5.0,   # пружина ↑ + инсайдер покупает (сильнейший)
    "insider_cluster":  4.0,   # ≥2 инсайдера в одну сторону
    "spring_fired":     3.0,   # разжатие пружины (пробой)
    "fund_consensus":   3.0,   # 2+ фонда двигают твою бумагу
    "macro_spike":      2.0,   # резкое движение сырья/ставок
    "spring_loaded":    1.0,   # взведённая пружина (готовится)
}
STRONG_CLUSTER_MULT = 1.5      # множитель для кластера ≥3 инсайдеров
CYCLE_BULL = {"Восстановление", "Экспансия"}
CYCLE_BEAR = {"Рецессия"}
STRENGTH = [(5.0, "высокая"), (3.0, "средняя"), (0.0, "низкая")]
# ──────────────────────────────────────────────────────────────


def _load(name):
    p = OUT / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _strength(score):
    for thr, label in STRENGTH:
        if score >= thr:
            return label
    return "низкая"


def _sig(cat, subject, direction, base, title, rationale, source, mult=1.0):
    return {"category": cat, "subject": subject, "direction": direction,
            "score": round(WEIGHTS.get(cat, 1.0) * mult, 1),
            "title": title, "rationale": rationale, "source": source}


def from_fertilizers(d, out):
    for tk, r in (d.get("producers") or {}).items():
        sp = r.get("spring", {})
        name = r.get("name", tk)
        if r.get("confluence"):
            b = (r.get("insider_buys") or [{}])[0]
            out.append(_sig("confluence", f"${tk}", "bull",
                            WEIGHTS["confluence"],
                            f"Совпадение: пружина ↑ + инсайдер покупает — {tk}",
                            f"Разжатие вверх на фоне покупки инсайдера ({b.get('insider','?')}). "
                            f"Цикл: {r.get('cycle',{}).get('phase','')}.",
                            "Удобрения v2"))
        elif sp.get("fired"):
            up = sp.get("coil_dir") == "вверх"
            vol = f", объём ×{sp.get('vol_ratio')}" if sp.get("vol_confirm") else ""
            out.append(_sig("spring_fired", f"${tk}", "bull" if up else "bear",
                            WEIGHTS["spring_fired"],
                            f"Пружина разжалась {'↑' if up else '↓'} — {tk}",
                            f"Пробой диапазона после сжатия{vol}. Фаза: {r.get('cycle',{}).get('phase','')}.",
                            "Удобрения v2", mult=1.3 if sp.get("vol_confirm") else 1.0))
        elif r.get("accumulation"):
            out.append(_sig("spring_loaded", f"${tk}", "bull",
                            WEIGHTS["spring_loaded"],
                            f"Накопление: пружина взведена ↑ + инсайдер покупает — {tk}",
                            "Сетап зреет: сжатие волатильности вверх и покупка инсайдера.",
                            "Удобрения v2"))
        elif sp.get("squeeze"):
            out.append(_sig("spring_loaded", f"${tk}", "neutral",
                            WEIGHTS["spring_loaded"],
                            f"Пружина взведена ({sp.get('coil_dir','?')}) — {tk}",
                            f"Волатильность сжата (BBW {sp.get('bbw_pctile')}%), ждём пробой.",
                            "Удобрения v2"))


def from_insiders(d, out):
    for c in (d.get("clusters") or []):
        buy = c.get("direction") == "buy"
        mult = STRONG_CLUSTER_MULT if c.get("count", 0) >= 3 else 1.0
        out.append(_sig("insider_cluster", f"${c['ticker']}", "bull" if buy else "bear",
                        WEIGHTS["insider_cluster"],
                        f"Кластер инсайдеров: {'покупки' if buy else 'продажи'} — {c['ticker']}",
                        f"{c['count']} разных инсайдера за 30д, суммарно "
                        f"~${abs(c.get('total_value',0))/1e6:.1f}M.",
                        "Инсайдеры (Form 4)", mult=mult))


def from_funds(d, out):
    # Пересечение движений фондов с портфелем: нетто наращивание/сокращение.
    funds = d.get("funds") or {}
    tally = {}   # tk -> {"in":set,"out":set}
    for name, f in funds.items():
        if f.get("error"):
            continue
        m = f.get("moves", {})
        for grp, key in (("new", "in"), ("increased", "in"),
                         ("exited", "out"), ("decreased", "out")):
            for x in m.get(grp, []):
                iss = (x.get("issuer", "") or "").upper()
                for tk, needle in PORTFOLIO_NAMES.items():
                    if needle in iss:
                        t = tally.setdefault(tk, {"in": set(), "out": set()})
                        t[key].add(name)
    for tk, t in tally.items():
        net = len(t["in"]) - len(t["out"])
        total = len(t["in"] | t["out"])
        if total < 2:
            continue  # консенсус = минимум 2 разных фонда
        direction = "bull" if net > 0 else ("bear" if net < 0 else "neutral")
        out.append(_sig("fund_consensus", f"${tk}", direction,
                        WEIGHTS["fund_consensus"],
                        f"Консенсус фондов по {tk}: нетто {net:+d}",
                        f"{len(t['in'])} наращивают / {len(t['out'])} сокращают "
                        f"(из {total} фондов, двигавших бумагу).",
                        "Движения фондов"))


MACRO_DIR = {  # ключ → (направление для рынка акций, короткая заметка)
    "brent": ("bear", "нефть вверх — инфляционное давление; плюс энергетике/HAL"),
    "wti":   ("bear", "нефть вверх — давление на маржу; плюс энергетике"),
    "natgas": ("neutral", "газ — сырьё для азотки"),
    "gold":  ("bear", "золото вверх — risk-off, защитный бид (NEM)"),
    "wheat": ("neutral", "зерно — продовольственный тезис (BG/ADM)"),
    "corn":  ("neutral", "зерно — продовольственный тезис"),
    "agri":  ("bull", "агробизнес растёт — удобренческий тезис"),
    "dxy":   ("bear", "доллар вверх — встречный ветер сырью и EM"),
}


def from_macro(d, out):
    for i in (d.get("indicators") or []):
        if not i.get("alert"):
            continue
        up = (i.get("change_1d") or 0) > 0
        base_dir, note = MACRO_DIR.get(i.get("key"), ("neutral", ""))
        # для золота/доллара «вверх» = risk-off; знак учитываем
        direction = base_dir if up else ("bull" if base_dir == "bear" else "neutral")
        out.append(_sig("macro_spike", i.get("label", "?"), direction,
                        WEIGHTS["macro_spike"],
                        f"Макро-скачок: {i.get('label')} {'+' if up else ''}{i.get('change_1d')}%",
                        note, "Макро"))


def market_posture(cyc, out):
    """Общая «поза» из фазы цикла + чистого крена сигналов."""
    phase = (cyc or {}).get("phase")
    if phase in CYCLE_BULL:
        pdir, plabel = "bull", f"Risk-on · {phase}"
    elif phase in CYCLE_BEAR:
        pdir, plabel = "bear", f"Risk-off · {phase}"
    elif phase:
        pdir, plabel = "neutral", f"Осторожно · {phase}"
    else:
        pdir, plabel = "neutral", "Нет данных цикла"
    tilt = (cyc or {}).get("lead", [])
    note = (cyc or {}).get("note", "")
    return {"label": plabel, "direction": pdir, "tilt": tilt, "note": note,
            "phase": phase, "score": (cyc or {}).get("score")}


def main():
    print("→ Слой сигналов: синтез из готовых данных")
    funds = _load("funds.json")
    insiders = _load("insiders.json")
    fert = _load("fertilizers.json")
    macro = _load("macro.json")
    cycles = _load("cycles.json")

    signals = []
    if fert:
        from_fertilizers(fert, signals)
    if insiders:
        from_insiders(insiders, signals)
    if funds:
        from_funds(funds, signals)
    if macro:
        from_macro(macro, signals)

    # ранжируем: сильные вверх, при равенстве — bull выше
    dir_rank = {"bull": 0, "neutral": 1, "bear": 2}
    signals.sort(key=lambda s: (-s["score"], dir_rank.get(s["direction"], 1)))
    for i, s in enumerate(signals, 1):
        s["rank"] = i
        s["strength"] = _strength(s["score"])

    counts = {"bull": 0, "bear": 0, "neutral": 0}
    for s in signals:
        counts[s["direction"]] = counts.get(s["direction"], 0) + 1

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "posture": market_posture(cycles, signals),
        "counts": counts,
        "signals": signals,
    }
    (OUT / "signals.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(f"✓ Сигналов: {len(signals)} (bull {counts['bull']} / "
          f"bear {counts['bear']} / neutral {counts['neutral']}) · "
          f"поза: {out['posture']['label']}")
    for s in signals[:8]:
        print(f"  [{s['strength']}] {s['direction']:7} {s['title']}")


if __name__ == "__main__":
    main()
