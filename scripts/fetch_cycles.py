#!/usr/bin/env python3
"""
Экономический цикл — макро-разложение делового цикла для рынка (S&P 500).

Воспроизводит методологию вложенных циклов:
  • Кондратьев (45–60 лет)  — технологический / долгосрочный (оценочно, качественно);
  • Жюгляр    (7–11 лет)    — деловой цикл (кривая доходности, кредитные спреды,
                              загрузка мощностей, безработица, промпроизводство);
  • Китчин    (2–4 года)    — цикл запасов (запасы/продажи, заявки на пособие,
                              разрешения на строительство, настроения).

Каждый индикатор даёт сигнал −1/0/+1. Их сумма (композит) + признаки поздней
фазы (инверсия кривой, рост заявок) → фаза: Восстановление → Экспансия →
Замедление → Рецессия. К фазе привязана секторная ротация (playbook).

Данные — бесплатный keyless CSV FRED (fredgraph.csv?id=...), без API-ключа.

ВАЖНО (честно): это вероятностный инструмент и иллюстрация методологии, НЕ
инвест-рекомендация. Фазы надёжно датируются лишь задним числом; К-цикл не имеет
строгого научного консенсуса; макроданные выходят с лагом. Не сигнал к сделке.

Только стандартная библиотека.
"""

import json
from datetime import date, datetime, timezone

from fetch_13f import OUT
import marketdata
import notify

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
# DBnomics зеркалит серии FRED через JSON-API без ключа и, в отличие от самого
# FRED, отвечает с IP GitHub Actions (FRED оттуда стабильно таймаутит).
DBNOMICS = "https://api.db.nomics.world/v22/series/FRED/{id}?observations=1"

# id FRED, человекочитаемый label, цикл, единица.
INDICATORS = [
    # Жюгляр — деловой цикл
    {"id": "T10Y2Y",       "label": "Кривая доходности 10Y−2Y", "cycle": "Жюгляр", "unit": "п.п."},
    {"id": "BAMLH0A0HYM2", "label": "Кредитный спред HY (OAS)",  "cycle": "Жюгляр", "unit": "%"},
    {"id": "TCU",          "label": "Загрузка мощностей",         "cycle": "Жюгляр", "unit": "%"},
    {"id": "UNRATE",       "label": "Безработица",                "cycle": "Жюгляр", "unit": "%"},
    {"id": "INDPRO",       "label": "Пром. производство (г/г)",   "cycle": "Жюгляр", "unit": "idx"},
    # Китчин — цикл запасов
    {"id": "ISRATIO",      "label": "Запасы / продажи",           "cycle": "Китчин", "unit": "x"},
    {"id": "ICSA",         "label": "Заявки на пособие",          "cycle": "Китчин", "unit": "тыс"},
    {"id": "PERMIT",       "label": "Разрешения на строительство","cycle": "Китчин", "unit": "тыс"},
    {"id": "UMCSENT",      "label": "Настроения потребителей",    "cycle": "Китчин", "unit": "idx"},
    # Кондратьев — долгосрочный контекст (качественно)
    {"id": "DFII10",       "label": "Реальная ставка 10Y (TIPS)", "cycle": "Кондратьев", "unit": "%"},
]

# Секторная ротация по фазам (из методологии). allocation в % на класс активов.
PHASE_PLAYBOOK = {
    "Восстановление": {
        "allocation": {"Циклические": 40, "Технологии": 30, "Сырьё": 20, "Защитные": 10},
        "lead": ["Финансы", "Промышленность", "Материалы"],
        "lag": ["Коммунальные услуги", "Товары первой необходимости"],
        "note": "Дно позади: PMI разворачивается вверх, ставки/спреды снижаются.",
    },
    "Экспансия": {
        "allocation": {"Циклические": 30, "Технологии": 40, "Сырьё": 20, "Защитные": 10},
        "lead": ["Технологии", "Дискреционные товары", "Здравоохранение"],
        "lag": ["Энергетика", "Коммунальные услуги"],
        "note": "Рост прибылей и мощностей; следи за перегревом и инверсией кривой.",
    },
    "Замедление": {
        "allocation": {"Циклические": 20, "Технологии": 20, "Сырьё": 30, "Защитные": 30},
        "lead": ["Здравоохранение", "Товары первой необходимости", "Коммунальные услуги"],
        "lag": ["Финансы", "Промышленность"],
        "note": "Пик пройден: рост замедляется, монетарная политика жёстче.",
    },
    "Рецессия": {
        "allocation": {"Циклические": 10, "Технологии": 10, "Сырьё": 30, "Защитные": 50},
        "lead": ["Коммунальные услуги", "Товары первой необходимости", "Защитные"],
        "lag": ["Дискреционные товары", "Материалы"],
        "note": "Спад: облигации/кэш/защита; жди стабилизации опережающих индикаторов.",
    },
}


def _ord(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d)).toordinal()


def parse_fred(csv_text):
    """CSV FRED (DATE,VALUE) → [(date_str, float)] старые→новые; '.' пропускаем."""
    out = []
    lines = [ln for ln in csv_text.strip().splitlines() if ln]
    if not lines or "," not in lines[0]:
        return out
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if v in (".", "", "NaN"):
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    return out


def fetch_series(fred_id):
    """Дневной/мес. ряд FRED-серии: DBnomics (осн.) → FRED CSV (фолбэк).

    Возвращает (series [(date,float)] старые→новые, source|reason)."""
    db_err = "dbnomics: пусто"
    try:
        d = json.loads(marketdata._raw(DBNOMICS.format(id=fred_id), timeout=15, retries=1))
        docs = (d.get("series") or {}).get("docs") or []
        if docs:
            periods = docs[0].get("period") or []
            values = docs[0].get("value") or []
            out = []
            for p, v in zip(periods, values):
                if v in (None, "NA", "."):
                    continue
                if len(p) == 7:      # месячные периоды 'YYYY-MM' → 'YYYY-MM-01'
                    p = p + "-01"
                try:
                    out.append((p, float(v)))
                except (ValueError, TypeError):
                    continue
            if len(out) >= 2:
                return out, "dbnomics"
    except Exception as e:
        db_err = f"dbnomics: {str(e)[:50]}"
    # фолбэк на FRED CSV (может таймаутить с IP Actions — короткий таймаут)
    try:
        series = parse_fred(marketdata._raw(FRED_CSV.format(id=fred_id), timeout=12, retries=1))
        if len(series) >= 2:
            return series, "fred"
        return [], f"{db_err}; fred: мало данных"
    except Exception as e:
        return [], f"{db_err}; fred: {str(e)[:40]}"


def val_ago(series, days):
    """Значение примерно N дней назад (последнее наблюдение не позже даты-цели)."""
    if not series:
        return None
    target = _ord(series[-1][0]) - days
    best = series[0][1]
    for ds, v in series:
        if _ord(ds) <= target:
            best = v
        else:
            break
    return best


def signal_for(ind_id, series):
    """Возвращает (signal ∈ {-1,0,1}, note, latest, prior, date)."""
    latest = series[-1][1]
    ldate = series[-1][0]
    prior = val_ago(series, 90)
    yoy = val_ago(series, 365)
    s, note = 0, ""

    if ind_id == "T10Y2Y":
        if latest < 0:
            s, note = -1, "инверсия — исторически опережает рецессию на 6–18 мес"
        elif latest > 0.2:
            s, note = 1, "положительный наклон — здоровый режим"
        else:
            s, note = 0, "около нуля — уплощение"
    elif ind_id == "BAMLH0A0HYM2":
        if latest > 5.0:
            s, note = -1, "спред >500 б.п. — кредитный стресс"
        elif latest < 3.5:
            s, note = 1, "узкий спред — аппетит к риску"
        else:
            s, note = 0, "нейтрально"
        if prior and latest > prior + 0.5:
            s, note = -1, "спреды расширяются — рост стресса"
    elif ind_id == "TCU":
        if prior is not None and latest > prior + 0.2 and latest > 77:
            s, note = 1, "загрузка растёт — экспансия"
        elif prior is not None and latest < prior - 0.3:
            s, note = -1, "загрузка падает — замедление"
        else:
            s, note = 0, "стабильно"
    elif ind_id == "UNRATE":
        if prior is not None and latest < prior - 0.1:
            s, note = 1, "безработица снижается"
        elif prior is not None and latest > prior + 0.1:
            s, note = -1, "безработица растёт — поздняя фаза"
        else:
            s, note = 0, "без изменений"
    elif ind_id == "INDPRO":
        if yoy:
            g = (latest - yoy) / yoy * 100
            note = f"{g:+.1f}% г/г"
            s = 1 if g > 1 else (-1 if g < -1 else 0)
    elif ind_id == "ISRATIO":
        if prior is not None and latest < prior - 0.01:
            s, note = 1, "запасы снижаются — впереди пополнение (плюс циклам)"
        elif prior is not None and latest > prior + 0.01:
            s, note = -1, "запасы растут — затоваривание"
        else:
            s, note = 0, "стабильно"
    elif ind_id == "ICSA":
        if prior is not None and latest < prior * 0.98:
            s, note = 1, "заявки снижаются — рынок труда крепнет"
        elif prior is not None and latest > prior * 1.02:
            s, note = -1, "заявки растут — ухудшение"
        else:
            s, note = 0, "стабильно"
    elif ind_id == "PERMIT":
        if prior is not None and latest > prior * 1.02:
            s, note = 1, "разрешения растут — опережающий плюс"
        elif prior is not None and latest < prior * 0.98:
            s, note = -1, "разрешения падают — охлаждение"
        else:
            s, note = 0, "стабильно"
    elif ind_id == "UMCSENT":
        if prior is not None and latest > prior * 1.03:
            s, note = 1, "настроения улучшаются"
        elif prior is not None and latest < prior * 0.97:
            s, note = -1, "настроения ухудшаются"
        else:
            s, note = 0, "стабильно"
    elif ind_id == "DFII10":
        # К-цикл: контекст, не голосует в композите
        trend = "растёт" if (prior is not None and latest > prior) else "снижается"
        s, note = 0, f"реальная ставка {trend} — фон стоимости капитала"

    return s, note, latest, prior, ldate


def phase_from(score, curve_inverted, claims_rising):
    if score <= -3 or (curve_inverted and claims_rising and score <= 0):
        return "Рецессия"
    if score >= 3 and not curve_inverted:
        return "Экспансия"
    if score >= 1:
        return "Восстановление"
    return "Замедление"


def main():
    print("→ Экономический цикл (DBnomics → FRED)")
    cycles = {"Жюгляр": [], "Китчин": [], "Кондратьев": []}
    errors = {}
    score = 0
    curve_inverted = False
    claims_rising = False

    for cfg in INDICATORS:
        series, src = fetch_series(cfg["id"])
        if len(series) < 2:
            errors[cfg["label"]] = src
            print(f"  ✗ {cfg['label']}: {src}")
            continue
        s, note, latest, prior, ldate = signal_for(cfg["id"], series)
        if cfg["cycle"] != "Кондратьев":
            score += s
        if cfg["id"] == "T10Y2Y":
            curve_inverted = latest < 0
        if cfg["id"] == "ICSA":
            claims_rising = prior is not None and latest > prior
        cycles[cfg["cycle"]].append({
            "id": cfg["id"], "label": cfg["label"], "unit": cfg["unit"],
            "value": round(latest, 2), "signal": s, "note": note, "date": ldate,
        })
        arrow = "▲" if s > 0 else ("▼" if s < 0 else "•")
        print(f"  {arrow} {cfg['label']}: {latest} [{cfg['cycle']}] {note}")

    have_any = any(cycles.values())
    phase = phase_from(score, curve_inverted, claims_rising) if have_any else None
    play = PHASE_PLAYBOOK.get(phase, {}) if phase else {}

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "score": score,
        "curve_inverted": curve_inverted,
        "cycles": cycles,
        "allocation": play.get("allocation", {}),
        "lead": play.get("lead", []),
        "lag": play.get("lag", []),
        "note": play.get("note", ""),
        "kondratieff_note": ("Оценочно: переход в повышательную фазу 6-го К-цикла "
                             "(ИИ + зелёная энергетика). Датировка К-циклов спорна."),
        "errors": errors,
    }

    path = OUT / "cycles.json"
    prev_phase = None
    if path.exists():
        try:
            prev_phase = json.loads(path.read_text(encoding="utf-8")).get("phase")
        except Exception:
            pass

    if not have_any and path.exists():
        print("→ Ничего не собрано — оставляю прежний cycles.json.")
    else:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ Сохранено в {path} · фаза: {phase} · скор: {score:+d}")

    # Telegram — только при смене фазы (это редкое и значимое событие).
    if phase and prev_phase and phase != prev_phase:
        e = notify.esc
        alloc = " · ".join(f"{k} {v}%" for k, v in out["allocation"].items())
        notify.send(
            f"🌀 <b>СМЕНА ФАЗЫ ЦИКЛА</b>: {e(prev_phase)} → <b>{e(phase)}</b>\n"
            f"Композит: {score:+d}{' · инверсия кривой' if curve_inverted else ''}\n"
            f"Ротация: {e(alloc)}\n"
            f"Лидеры: {e(', '.join(out['lead']))}\n"
            f"<i>Методология делового цикла, не рекомендация.</i>")
        print(f"  [tg] смена фазы: {prev_phase} → {phase}")


if __name__ == "__main__":
    main()
