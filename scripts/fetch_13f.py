#!/usr/bin/env python3
"""
SEC EDGAR 13F parser — полностью бесплатный, без API-ключей.
Тянет последние 13F-HR указанных фондов, парсит холдинги,
считает изменения квартал-к-кварталу, сохраняет в docs/data/.

SEC требует: User-Agent с контактом + не более 10 запросов/сек.
"""

import json
import time
import re
import gzip
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import xml.etree.ElementTree as ET

import notify

# ─────────────────────────────────────────────────────────────
# КОНФИГ: впиши свой email (требование SEC), и список фондов по CIK.
# CIK находишь на sec.gov → поиск компании. Ведущие нули можно опускать.
# ─────────────────────────────────────────────────────────────
SEC_CONTACT = "aihohonono@gmail.com"   # ← ОБЯЗАТЕЛЬНО замени на свой email

FUNDS = {
    "Berkshire Hathaway": "0001067983",
    "Bridgewater":        "0001350694",
    "Whale Rock":         "0001387322",
    "Octahedron":         "0001767640",
}

# Твои тикеры — для вкладки "пересечения". Меняй под себя.
MY_PORTFOLIO = ["AMKBY","CLSK","CIG","GNL","HAL","HUT","KEEL","KC",
                "NBIS","NEM","PSTL","RIOT","CLM","KWEB"]

# Тикер → подстрока названия эмитента в 13F (13F не содержит тикеров).
# Зеркало PORTFOLIO_NAMES из docs/index.html — держи в синхроне.
PORTFOLIO_NAMES = {
    "KWEB": "KRANESHARES", "NEM": "NEWMONT", "HAL": "HALLIBURTON", "AMKBY": "MAERSK",
    "NBIS": "NEBIUS", "GNL": "GLOBAL NET LEASE", "RIOT": "RIOT", "HUT": "HUT 8",
    "CLSK": "CLEANSPARK", "KC": "KINGSOFT", "CIG": "ENERGETICA", "PSTL": "POSTAL REALTY",
    "CLM": "CORNERSTONE", "KEEL": "KEEL",
}

HEADERS = {
    "User-Agent": f"smart-money-dashboard/1.0 ({SEC_CONTACT})",
}
OUT = Path(__file__).resolve().parent.parent / "docs" / "data"
OUT.mkdir(parents=True, exist_ok=True)


def get(url, is_json=False):
    """GET с дросселированием под лимиты SEC."""
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        content_encoding = r.headers.get("Content-Encoding", "")
    time.sleep(0.25)  # < 10 req/sec с запасом
    # Декомпрессия на случай, если сервер всё равно вернул gzip
    if content_encoding == "gzip" or (raw[:2] == b"\x1f\x8b"):
        raw = gzip.decompress(raw)
    if is_json:
        return json.loads(raw.decode("utf-8"))
    return raw.decode("utf-8", "ignore")


def latest_13f_filings(cik, n=2):
    """Возвращает последние n подач 13F-HR: список (accession, filing_date, report_date)."""
    cik10 = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    data = get(url, is_json=True)
    recent = data["filings"]["recent"]
    out = []
    for form, acc, fdate, rdate, doc in zip(
        recent["form"], recent["accessionNumber"], recent["filingDate"],
        recent["reportDate"], recent["primaryDocument"]):
        if form == "13F-HR":
            out.append({"accession": acc.replace("-", ""), "filing_date": fdate,
                        "report_date": rdate})
            if len(out) >= n:
                break
    return out


def find_info_table(cik, accession):
    """Находит URL XML information table внутри подачи."""
    cik_int = str(int(cik))
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}"
    idx = get(f"{base}/", is_json=False)
    # ищем .xml файлы, исключая primary_doc
    xmls = re.findall(r'href="([^"]+\.xml)"', idx)
    cands = [x.split("/")[-1] for x in xmls if "primary_doc" not in x.lower()]
    # информационная таблица обычно самая большая / содержит 'form13f' или 'table'
    for name in cands:
        low = name.lower()
        if "table" in low or "form13f" in low or "infotable" in low:
            return f"{base}/{name}"
    return f"{base}/{cands[0]}" if cands else None


def parse_info_table(xml_text):
    """Парсит holdings из 13F information table XML."""
    # Убираем namespace: сначала prefixed-атрибуты (xsi:schemaLocation etc),
    # потом xmlns-декларации, потом префиксы тегов (n1:infoTable → infoTable)
    xml_text = re.sub(r'\s+\w+:\w+="[^"]*"', " ", xml_text)  # xsi:attr="..." → удалить
    xml_text = re.sub(r'xmlns(:\w+)?="[^"]+"', "", xml_text)  # xmlns declarations
    xml_text = re.sub(r'<(/?)\w+:', r'<\1', xml_text)         # <n1:tag> → <tag>
    holdings = defaultdict(lambda: {"shares": 0, "value": 0})
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  XML ParseError: {e}")
        return {}
    for it in root.iter("infoTable"):
        issuer = (it.findtext("nameOfIssuer") or "").strip()
        cusip = (it.findtext("cusip") or "").strip()
        val = it.findtext("value") or "0"
        sh_node = it.find("shrsOrPrnAmt")
        shares = sh_node.findtext("sshPrnamt") if sh_node is not None else "0"
        try:
            val = int(float(val))
            shares = int(float(shares))
        except ValueError:
            continue
        key = cusip or issuer
        holdings[key]["issuer"] = issuer
        holdings[key]["cusip"] = cusip
        holdings[key]["shares"] += shares
        holdings[key]["value"] += val
    return dict(holdings)


def compute_changes(current, previous):
    """Сравнивает два квартала: new / increased / decreased / exited."""
    moves = {"new": [], "increased": [], "decreased": [], "exited": []}
    prev_keys = set(previous.keys())
    cur_keys = set(current.keys())
    for k in cur_keys:
        c = current[k]
        if k not in prev_keys:
            moves["new"].append({"issuer": c["issuer"], "value": c["value"], "shares": c["shares"]})
        else:
            ps = previous[k]["shares"]
            cs = c["shares"]
            if cs > ps * 1.05:
                pct = ((cs - ps) / ps * 100) if ps else 0
                moves["increased"].append({"issuer": c["issuer"], "value": c["value"], "pct": round(pct)})
            elif cs < ps * 0.95:
                pct = ((cs - ps) / ps * 100) if ps else 0
                moves["decreased"].append({"issuer": c["issuer"], "value": c["value"], "pct": round(pct)})
    for k in prev_keys - cur_keys:
        moves["exited"].append({"issuer": previous[k]["issuer"], "value": previous[k]["value"]})
    # сортируем по размеру
    for key in moves:
        moves[key].sort(key=lambda x: x.get("value", 0), reverse=True)
        moves[key] = moves[key][:15]
    return moves


def normalize_values(holdings, report_date):
    """
    До 2023Q1 SEC писал value в тысячах долларов, после — в полных долларах.
    Эвристика: если отчёт раньше 2023-01-01, умножаем на 1000.
    """
    try:
        year = int(report_date[:4])
    except (ValueError, TypeError):
        return holdings
    if year < 2023:
        for h in holdings.values():
            h["value"] *= 1000
    return holdings


def top_holdings(holdings, n=10):
    total = sum(h["value"] for h in holdings.values()) or 1
    rows = sorted(holdings.values(), key=lambda x: x["value"], reverse=True)[:n]
    return [{"issuer": r["issuer"], "value": r["value"],
             "pct": round(r["value"] / total * 100, 2)} for r in rows]


def load_prev_report_dates():
    """report_date каждого фонда из уже лежащего funds.json (до перезаписи).

    Нужно, чтобы Telegram-дайджест 13F слался ТОЛЬКО когда фонд подал свежий
    квартал (report_date сдвинулся), а не на каждый ежедневный cron —
    funds.json меняется каждый день из-за поля generated, но холдинги те же.
    """
    path = OUT / "funds.json"
    if not path.exists():
        return None  # первый запуск — не спамим историей
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        return {name: f.get("report_date") for name, f in old.get("funds", {}).items()}
    except Exception:
        return {}


def fmt_usd(v):
    v = abs(v)
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.0f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def portfolio_hits(fund):
    """Ищет движения фонда, затрагивающие тикеры твоего портфеля.

    Возвращает список строк вида '$TICKER — NEWMONT: +43%'. Это самое ценное
    в дайджесте: не абстрактные движения фонда, а те, что пересекаются с тобой.
    """
    m = fund["moves"]
    labelled = (
        [("новая", x) for x in m.get("new", [])] +
        [((("+" if x.get("pct", 0) > 0 else "") + f"{x.get('pct', 0)}%"), x) for x in m.get("increased", [])] +
        [("полный выход", x) for x in m.get("exited", [])] +
        [(f"{x.get('pct', 0)}%", x) for x in m.get("decreased", [])]
    )
    hits = []
    for tk, needle in PORTFOLIO_NAMES.items():
        for label, x in labelled:
            if needle in (x.get("issuer", "") or "").upper():
                hits.append(f"${tk} — {x['issuer']}: {label}")
    return hits


def build_13f_digest(name, fund):
    """Короткий дайджест движений фонда за новый отчётный квартал."""
    e = notify.esc
    edgar = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
             f"&CIK={fund.get('cik','')}&type=13F-HR&dateb=&owner=include&count=10")
    lines = [f"🏦 <b>{e(name)}</b> подал новый 13F",
             f"Квартал {e(fund['report_date'])} · подан {e(fund['filing_date'])} · "
             f"{fund['positions']} позиций · {fmt_usd(fund['total_value'])}"]

    # пересечение с портфелем — вверх, крупным планом
    hits = portfolio_hits(fund)
    if hits:
        lines.append("\n⚡ <b>Затрагивает твой портфель</b>")
        for h in hits[:8]:
            lines.append(f"  • {e(h)}")

    m = fund["moves"]
    new, inc = m.get("new", []), m.get("increased", [])
    exited, dec = m.get("exited", []), m.get("decreased", [])
    if new or inc:
        lines.append("\n↗️ <b>Зашёл / нарастил</b>")
        for x in new[:5]:
            lines.append(f"  • {e(x['issuer'])} — новая, {fmt_usd(x['value'])}")
        for x in inc[:5]:
            sign = "+" if x.get("pct", 0) > 0 else ""
            lines.append(f"  • {e(x['issuer'])} — {sign}{x['pct']}%, {fmt_usd(x['value'])}")
    if exited or dec:
        lines.append("\n↘️ <b>Вышел / сократил</b>")
        for x in exited[:5]:
            lines.append(f"  • {e(x['issuer'])} — полный выход, было {fmt_usd(x['value'])}")
        for x in dec[:5]:
            lines.append(f"  • {e(x['issuer'])} — {x['pct']}%, {fmt_usd(x['value'])}")

    lines.append(f"\n<a href=\"{e(edgar)}\">Подача на SEC EDGAR</a>")
    lines.append("<i>13F: только длинные позиции США, задержка до 45 дней. "
                 "Карта прошлого, не сигнал в реальном времени.</i>")
    return "\n".join(lines)


def main():
    prev_dates = load_prev_report_dates()
    result = {"generated": datetime.now(timezone.utc).isoformat(),
              "my_portfolio": MY_PORTFOLIO, "funds": {}}
    for name, cik in FUNDS.items():
        print(f"→ {name} (CIK {cik})")
        try:
            filings = latest_13f_filings(cik, n=2)
            if not filings:
                print(f"  нет 13F-HR"); continue
            cur_url = find_info_table(cik, filings[0]["accession"])
            current = parse_info_table(get(cur_url)) if cur_url else {}
            current = normalize_values(current, filings[0]["report_date"])
            previous = {}
            if len(filings) > 1:
                prev_url = find_info_table(cik, filings[1]["accession"])
                previous = parse_info_table(get(prev_url)) if prev_url else {}
                previous = normalize_values(previous, filings[1]["report_date"])
            result["funds"][name] = {
                "cik": cik,
                "report_date": filings[0]["report_date"],
                "filing_date": filings[0]["filing_date"],
                "total_value": sum(h["value"] for h in current.values()),
                "positions": len(current),
                "top_holdings": top_holdings(current),
                "moves": compute_changes(current, previous),
            }
            print(f"  ✓ {len(current)} позиций, отчёт {filings[0]['report_date']}")
        except Exception as e:
            import traceback
            print(f"  ✗ ошибка: {e}")
            traceback.print_exc()
            result["funds"][name] = {"error": str(e)}
    (OUT / "funds.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n✓ Сохранено в {OUT/'funds.json'}")

    # Telegram-дайджест только для фондов, подавших свежий квартал.
    if prev_dates is None:
        print("→ Первый запуск 13F: дайджесты не шлём (посев).")
    else:
        sent = 0
        for name, fund in result["funds"].items():
            if fund.get("error"):
                continue
            rd = fund.get("report_date")
            old_rd = prev_dates.get(name)
            # шлём, если фонд новый в конфиге ИЛИ отчётный период сдвинулся
            if rd and rd != old_rd:
                if notify.send(build_13f_digest(name, fund)):
                    sent += 1
                    print(f"  [tg] дайджест 13F отправлен: {name} ({old_rd} → {rd})")
        if sent == 0:
            print("→ Свежих 13F-подач нет — Telegram молчит (это норма между кварталами).")


if __name__ == "__main__":
    if SEC_CONTACT == "your-email@example.com":
        print("⚠ ВПИШИ свой email в SEC_CONTACT — SEC блокирует запросы без контакта.")
    main()
