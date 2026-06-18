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
    # убираем namespace для простоты
    xml_text = re.sub(r'xmlns(:\w+)?="[^"]+"', "", xml_text)
    xml_text = re.sub(r'<(/?)\w+:', r'<\1', xml_text)
    holdings = defaultdict(lambda: {"shares": 0, "value": 0})
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
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


def main():
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


if __name__ == "__main__":
    if SEC_CONTACT == "your-email@example.com":
        print("⚠ ВПИШИ свой email в SEC_CONTACT — SEC блокирует запросы без контакта.")
    main()
