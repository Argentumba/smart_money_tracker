#!/usr/bin/env python3
"""
Макро-индикаторы — ранние сигналы смены рыночного нарратива.

13F/Form 4 говорят о конкретных бумагах. Но разворот risk-on↔risk-off часто
виден раньше в сырье, энергии и долларе. Этот скрипт тянет несколько ключевых
серий с бесплатного источника Stooq (CSV, без API-ключей — в духе проекта),
считает дневное и недельное изменение, пишет docs/data/macro.json и шлёт в
Telegram алерт при резком движении.

Честная оговорка: бенчмарк карбамида (удобрения) и ставки танкеров (Baltic
Dirty Tanker) бесплатного надёжного API не имеют — они добавлены в разделе
manual_watch как ссылки «смотреть вручную», а не выдуманными числами.

Только стандартная библиотека. Сеть недоступна из некоторых датацентров —
скрипт при ошибке источника не падает, просто оставляет индикатор пустым.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fetch_13f import get, OUT
import notify

# Отслеживаем ликвидные commodity-ETF (тикеры Stooq с суффиксом .us стабильны и
# проверяемы), а не фьючерсные continuation-символы — те на Stooq капризны.
# Показываем реальную цену пая ETF; сигнал несёт % изменения, а он повторяет
# базовый актив. key, label (с тикером), символ Stooq, единица, порог алерта (|Δ день| %).
INDICATORS = [
    {"key": "brent",  "label": "Нефть Brent (BNO)",   "symbol": "bno.us",  "unit": "$", "alert": 5.0},
    {"key": "wti",    "label": "Нефть WTI (USO)",     "symbol": "uso.us",  "unit": "$", "alert": 5.0},
    {"key": "natgas", "label": "Природный газ (UNG)", "symbol": "ung.us",  "unit": "$", "alert": 8.0},
    {"key": "gold",   "label": "Золото (GLD)",        "symbol": "gld.us",  "unit": "$", "alert": 3.0},
    {"key": "wheat",  "label": "Пшеница (WEAT)",      "symbol": "weat.us", "unit": "$", "alert": 5.0},
    {"key": "corn",   "label": "Кукуруза (CORN)",     "symbol": "corn.us", "unit": "$", "alert": 5.0},
    {"key": "agri",   "label": "Агробизнес (MOO)",    "symbol": "moo.us",  "unit": "$", "alert": 4.0},
    {"key": "dxy",    "label": "Доллар (UUP)",        "symbol": "uup.us",  "unit": "$", "alert": 1.5},
]

# Что важно, но бесплатного API нет — выводим ссылками на дашборде.
MANUAL_WATCH = [
    {"label": "Карбамид (Middle East urea FOB)",
     "why": "Прямой тепловизор удобренческого шока — цена азотки из Залива.",
     "url": "https://www.google.com/search?q=middle+east+urea+price+fob"},
    {"label": "Ставки танкеров (Baltic Dirty Tanker Index)",
     "why": "Дёргаются первыми при рисках вокруг Ормуза, до заголовков.",
     "url": "https://www.balticexchange.com/en/data-services/routes.html"},
    {"label": "Индекс продовольствия ФАО (FAO Food Price Index)",
     "why": "Итоговый барометр продбезопасности (запаздывает; фьючерсы зерна опережают).",
     "url": "https://www.fao.org/worldfoodsituation/foodpricesindex/en/"},
]

STOOQ_HIST = "https://stooq.com/q/d/l/?s={symbol}&i=d"


def parse_closes(csv_text):
    """Из CSV Stooq (Date,Open,High,Low,Close,Volume) → список (date, close), старые→новые."""
    rows = []
    lines = [ln for ln in csv_text.strip().splitlines() if ln]
    if not lines or not lines[0].lower().startswith("date"):
        return rows
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 5:
            continue
        date = parts[0]
        try:
            close = float(parts[4])
        except ValueError:
            continue
        rows.append((date, close))
    return rows


def pct(cur, ref):
    if ref in (None, 0):
        return None
    return round((cur - ref) / ref * 100, 2)


def fetch_indicator(cfg):
    """Возвращает dict индикатора или None при недоступности источника."""
    try:
        csv_text = get(STOOQ_HIST.format(symbol=cfg["symbol"]))
    except Exception as e:
        print(f"  ✗ {cfg['label']}: источник недоступен ({e})")
        return None
    closes = parse_closes(csv_text)
    if len(closes) < 2:
        print(f"  ✗ {cfg['label']}: мало данных")
        return None
    date, price = closes[-1]
    prev = closes[-2][1]
    week = closes[-6][1] if len(closes) >= 6 else None
    c1 = pct(price, prev)
    c5 = pct(price, week)
    alert = c1 is not None and abs(c1) >= cfg["alert"]
    print(f"  ✓ {cfg['label']}: {price} ({'+' if (c1 or 0) > 0 else ''}{c1}% день)")
    return {"key": cfg["key"], "label": cfg["label"], "unit": cfg["unit"],
            "price": price, "change_1d": c1, "change_5d": c5,
            "date": date, "alert": bool(alert)}


def implication(ind):
    """Короткая подсказка, что скачок значит для портфеля/тезиса."""
    k, up = ind["key"], (ind.get("change_1d") or 0) > 0
    hints = {
        "brent":  "нефть — плюс для HAL; шок Ормуза",
        "wti":    "нефть — плюс для HAL",
        "natgas": "газ — сырьё для азотки; давит на удобрения вне США",
        "gold":   "золото — защитный бид, плюс для NEM" if up else "золото вниз — risk-on",
        "wheat":  "зерно — продовольственный тезис (BG/ADM)",
        "corn":   "зерно — продовольственный тезис (BG/ADM)",
        "agri":   "агробизнес — прямой прокси удобренческого тезиса (CF/NTR/MOS/ADM)",
        "dxy":    "доллар вверх — давит на сырьё и EM" if up else "доллар вниз — попутный ветер сырью",
    }
    return hints.get(k, "")


def main():
    indicators = []
    alerts = []
    print("→ Сбор макро-индикаторов (Stooq)")
    for cfg in INDICATORS:
        ind = fetch_indicator(cfg)
        if ind:
            indicators.append(ind)
            if ind["alert"]:
                alerts.append(ind)

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "indicators": indicators,
        "manual_watch": MANUAL_WATCH,
    }
    # Если вообще ничего не собралось (источник заблокирован) — не затираем
    # возможный прежний файл пустышкой.
    path = OUT / "macro.json"
    if not indicators and path.exists():
        print("→ Ничего не собрано, оставляю прежний macro.json без изменений.")
    else:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✓ Сохранено в {path} ({len(indicators)} индикаторов)")

    # Telegram: по одному сообщению на резкий скачок.
    for ind in alerts:
        arrow = "🟢" if (ind.get("change_1d") or 0) > 0 else "🔴"
        e = notify.esc
        msg = (f"{arrow} <b>МАКРО-СКАЧОК</b> · {e(ind['label'])}\n"
               f"{ind['price']} {e(ind['unit'])} · день "
               f"{'+' if ind['change_1d'] > 0 else ''}{ind['change_1d']}% · "
               f"неделя {'+' if (ind['change_5d'] or 0) > 0 else ''}{ind['change_5d']}%\n"
               f"<i>{e(implication(ind))}</i>")
        notify.send(msg)
    if alerts:
        print(f"→ Отправлено макро-алертов: {len(alerts)}")
    else:
        print("→ Резких движений нет — Telegram молчит.")


if __name__ == "__main__":
    main()
