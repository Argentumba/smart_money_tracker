#!/usr/bin/env python3
"""
SEC EDGAR Form 4 монитор — инсайдерские сделки по тикерам портфеля.

Зачем: 13F приходит раз в квартал с задержкой до 45 дней — это карта прошлого.
Form 4 (сделки инсайдеров: CEO/CFO/директора/владельцы 10%+) подаётся в течение
2 РАБОЧИХ ДНЕЙ после сделки. Это самый быстрый публичный сигнал по конкретной
компании: когда инсайдеры начинают ПОКУПАТЬ или ПРОДАВАТЬ свои же акции.

Логика:
  1. Резолвим тикеры портфеля → CIK через SEC company_tickers.json.
     (ETF / иностранные ADR Form 4 не подают — просто отсеются.)
  2. Для каждого CIK берём свежие Form 4 из submissions API.
  3. Парсим ownership XML: кто, какая должность, покупка/продажа, сколько, по чём.
  4. Только новые подачи (по accession) → шлём в Telegram и пишем в insiders.json.

Первый запуск (файла ещё нет) — «посев»: помечаем текущие подачи как виденные
БЕЗ алертов, чтобы не вывалить историю. Дальше алертим только новое.

Только стандартная библиотека. Требования SEC: User-Agent с контактом, < 10 req/s.
"""

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

# Переиспользуем троттлящий GET, заголовки и список портфеля из основного скрипта.
from fetch_13f import get, MY_PORTFOLIO, OUT
import notify

# ─────────────────────────────────────────────────────────────
# Коды транзакций Form 4. По умолчанию алертим только «сделки убеждённости»:
#   P — открытая покупка на рынке (бычий сигнал)
#   S — открытая продажа на рынке (медвежий сигнал)
# Остальные (A=грант, M=исполнение опциона, F=налоговое удержание, G=дар...) —
# это компенсация/техника, шума много, сигнала мало. При желании расширь набор.
# ─────────────────────────────────────────────────────────────
ALERT_CODES = {"P", "S"}

CODE_LABELS = {
    "P": "Покупка на рынке",
    "S": "Продажа на рынке",
    "A": "Грант/начисление",
    "D": "Отчуждение эмитенту",
    "F": "Налоговое удержание",
    "M": "Исполнение опциона",
    "G": "Дар",
    "C": "Конвертация",
    "X": "Исполнение права",
}

# Сколько дней истории смотреть, и сколько последних подач держать в файле.
LOOKBACK_DAYS = 60
KEEP_TXNS = 60
KEEP_SEEN = 400

STATE_FILE = OUT / "insiders.json"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠ не смог прочитать {STATE_FILE}: {e}")
    return None


def load_ticker_map():
    """{TICKER: 'cik10'} из официального файла SEC."""
    data = get(TICKER_MAP_URL, is_json=True)
    out = {}
    for row in data.values():
        try:
            out[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
        except (KeyError, TypeError):
            continue
    return out


def recent_form4(cik10):
    """Список свежих Form 4 подач фонда: accession, даты, primaryDocument."""
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    data = get(url, is_json=True)
    recent = data["filings"]["recent"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    out = []
    for form, acc, fdate, pdoc in zip(
        recent["form"], recent["accessionNumber"],
        recent["filingDate"], recent["primaryDocument"]):
        if form == "4" and fdate >= cutoff:
            out.append({
                "accession": acc,
                "acc_nodash": acc.replace("-", ""),
                "filing_date": fdate,
                "primary": pdoc or "",
            })
    return out


def _form4_xml_candidates(cik10, filing):
    """Возможные URL сырого ownership XML внутри подачи."""
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/{filing['acc_nodash']}"
    cands = []
    pd = filing["primary"]
    if pd:
        # primaryDocument часто вида 'xslF345X05/wf-form4.xml' — сырой лежит без папки xsl
        cands.append(f"{base}/{pd.split('/')[-1]}")
        if "/" in pd:
            cands.append(f"{base}/{pd}")
    # запасной вариант: перечислить каталог и взять .xml
    cands.append(("__DIR__", base))
    return cands


def fetch_form4_xml(cik10, filing):
    """Возвращает распарсенный root ownershipDocument или None."""
    for cand in _form4_xml_candidates(cik10, filing):
        try:
            if isinstance(cand, tuple):  # обход каталога
                _, base = cand
                idx = get(f"{base}/", is_json=False)
                for name in re.findall(r'href="([^"]+\.xml)"', idx):
                    fname = name.split("/")[-1]
                    if "primary_doc" in fname.lower():
                        continue
                    root = _try_parse(get(f"{base}/{fname}"))
                    if root is not None:
                        return root
                continue
            root = _try_parse(get(cand))
            if root is not None:
                return root
        except Exception:
            continue
    return None


def _try_parse(xml_text):
    """Парсит и подтверждает, что это ownershipDocument (Form 3/4/5)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    tag = root.tag.split("}")[-1]  # на случай namespace
    return root if tag == "ownershipDocument" else None


def _txt(node, path):
    if node is None:
        return None
    found = node.findtext(path)
    return found.strip() if found else None


def parse_form4(root):
    """Достаём инсайдера, должность и не-деривативные транзакции."""
    owner = root.find("reportingOwner")
    name = _txt(owner, "reportingOwnerId/rptOwnerName") or "—"
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_dir = (_txt(rel, "isDirector") or "").lower() in ("1", "true")
    is_off = (_txt(rel, "isOfficer") or "").lower() in ("1", "true")
    is_ten = (_txt(rel, "isTenPercentOwner") or "").lower() in ("1", "true")
    title = _txt(rel, "officerTitle")
    if not title:
        roles = []
        if is_dir:
            roles.append("Директор")
        if is_ten:
            roles.append("Владелец 10%+")
        if is_off:
            roles.append("Топ-менеджер")
        title = ", ".join(roles) or "Инсайдер"

    # красивое имя: SEC пишет «Doe John Q» → «John Q Doe» не восстановить надёжно,
    # оставляем как есть, просто нормализуем регистр если ВСЁ КАПСОМ.
    if name.isupper():
        name = name.title()

    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _txt(t, "transactionCoding/transactionCode")
        if not code:
            continue
        shares = _txt(t, "transactionAmounts/transactionShares/value") or "0"
        price = _txt(t, "transactionAmounts/transactionPricePerShare/value") or "0"
        ad = _txt(t, "transactionAmounts/transactionAcquiredDisposedCode/value") or ""
        date = _txt(t, "transactionDate/value") or ""
        owned = _txt(t, "postTransactionAmounts/sharesOwnedFollowingTransaction/value")
        try:
            shares_n = float(shares)
            price_n = float(price)
        except ValueError:
            continue
        try:
            owned_n = float(owned) if owned is not None else None
        except ValueError:
            owned_n = None
        txns.append({
            "code": code,
            "shares": shares_n,
            "price": price_n,
            "value": shares_n * price_n,
            "acq_disp": ad,  # A=приобрёл, D=избавился
            "date": date,
            "owned_after": owned_n,
        })
    return {"insider": name, "title": title, "txns": txns}


def aggregate(txns):
    """Схлопываем транзакции одной подачи по коду: сумма акций, взвешенная цена.

    Дополнительно считаем долю пакета: сколько процентов своего холдинга инсайдер
    затронул этой сделкой (сильный маркер убеждённости — продать 5% пакета и
    продать 90% это очень разные сигналы). owned_after берём из последней по
    порядку транзакции кода (это итоговый остаток), а прежний размер пакета
    реконструируем от него.
    """
    by_code = {}
    for t in txns:
        if t["code"] not in ALERT_CODES:
            continue
        agg = by_code.setdefault(t["code"], {"shares": 0.0, "value": 0.0,
                                             "date": t["date"], "owned_after": None})
        agg["shares"] += t["shares"]
        agg["value"] += t["value"]
        if t.get("owned_after") is not None:
            agg["owned_after"] = t["owned_after"]  # последний по порядку = итоговый
    out = []
    for code, a in by_code.items():
        price = a["value"] / a["shares"] if a["shares"] else 0.0
        stake_pct = None
        owned = a["owned_after"]
        if owned is not None:
            # P (покупка) → прежний = итог − купленное; S (продажа) → прежний = итог + проданное
            prior = owned - a["shares"] if code == "P" else owned + a["shares"]
            if prior > 0:
                stake_pct = round(a["shares"] / prior * 100)
        out.append({"code": code, "shares": a["shares"], "value": a["value"],
                    "price": price, "date": a["date"],
                    "owned_after": owned, "stake_pct": stake_pct})
    return out


def fmt_usd(v):
    v = abs(v)
    if v >= 1e9:
        return f"${v/1e9:.1f}B"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def _shares(n):
    return f"{n:,.0f}".replace(",", " ")


def build_message(ticker, parsed, agg, url=None):
    lines = []
    for a in agg:
        buy = a["code"] == "P"
        head = "🟢 <b>ИНСАЙДЕР ПОКУПАЕТ</b>" if buy else "🔴 <b>ИНСАЙДЕР ПРОДАЁТ</b>"
        verb = "Купил" if buy else "Продал"
        price = f"${a['price']:,.2f}" if a["price"] else "—"

        # доля пакета — маркер убеждённости
        stake = ""
        if a.get("stake_pct") is not None:
            if buy:
                stake = f" (+{a['stake_pct']}% к пакету)"
            else:
                stake = " (продал весь пакет)" if a["stake_pct"] >= 99 else f" (−{a['stake_pct']}% пакета)"

        row = [
            f"{head} · ${notify.esc(ticker)}",
            f"<b>{notify.esc(parsed['insider'])}</b> — {notify.esc(parsed['title'])}",
            f"{verb} {_shares(a['shares'])} шт @ {price} ≈ <b>{fmt_usd(a['value'])}</b>{stake}",
        ]
        if a.get("owned_after") is not None:
            row.append(f"Осталось у инсайдера: {_shares(a['owned_after'])} шт")
        tail = f"Сделка {notify.esc(a['date'])} · Form 4"
        if url:
            tail += f" · <a href=\"{notify.esc(url)}\">SEC</a>"
        row.append(tail)
        lines.append("\n".join(row))
    return "\n\n".join(lines)


CLUSTER_WINDOW_DAYS = 30   # окно, в котором инсайдеры считаются «кластером»


def _direction(rec):
    codes = {t["code"] for t in rec.get("trades", [])}
    if "P" in codes:
        return "buy"
    if "S" in codes:
        return "sell"
    return None


def compute_clusters(txns):
    """Кластеры инсайдеров: ≥2 РАЗНЫХ инсайдера в одну сторону по одной бумаге
    за окно. Кластерные покупки/продажи — куда более сильный сигнал, чем одиночные.
    """
    from datetime import date as _date
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLUSTER_WINDOW_DAYS)).strftime("%Y-%m-%d")
    groups = {}
    for r in txns:
        if r.get("filing_date", "") < cutoff:
            continue
        d = _direction(r)
        if not d:
            continue
        key = (r["ticker"], d)
        g = groups.setdefault(key, {"insiders": {}, "value": 0.0, "dates": []})
        val = sum(t.get("value", 0) for t in r.get("trades", []))
        g["insiders"][r["insider"]] = g["insiders"].get(r["insider"], 0) + val
        g["value"] += val
        g["dates"].append(r.get("filing_date", ""))
    out = []
    for (ticker, direction), g in groups.items():
        names = list(g["insiders"].keys())
        if len(names) >= 2:
            out.append({
                "ticker": ticker, "direction": direction,
                "count": len(names), "insiders": names,
                "total_value": round(g["value"]),
                "last_date": max(g["dates"]) if g["dates"] else "",
            })
    out.sort(key=lambda c: (c["count"], c["total_value"]), reverse=True)
    return out


def build_cluster_alert(c):
    e = notify.esc
    buy = c["direction"] == "buy"
    head = "🟢🟢 <b>КЛАСТЕР ПОКУПОК ИНСАЙДЕРОВ</b>" if buy else "🔴🔴 <b>КЛАСТЕР ПРОДАЖ ИНСАЙДЕРОВ</b>"
    strong = " · <b>сильный сигнал</b>" if c["count"] >= 3 else ""
    return (f"{head} · ${e(c['ticker'])}{strong}\n"
            f"{c['count']} разных инсайдера за {CLUSTER_WINDOW_DAYS} дней: "
            f"{e(', '.join(c['insiders']))}\n"
            f"Суммарно ≈ <b>{fmt_usd(c['total_value'])}</b>\n"
            f"<i>Несколько инсайдеров в одну сторону — сильнее одиночной сделки. Не рекомендация.</i>")


def main():
    prev = load_state()
    seeding = prev is None
    seen = set(prev.get("seen_accessions", [])) if prev else set()
    kept_txns = prev.get("transactions", []) if prev else []
    seen_clusters = set(prev.get("seen_clusters", [])) if prev else set()

    if seeding:
        print("→ Первый запуск: посев без алертов (запоминаем текущие Form 4).")

    try:
        tmap = load_ticker_map()
    except Exception as e:
        print(f"✗ не удалось загрузить карту тикеров SEC: {e}")
        return

    new_seen = set(seen)
    fresh_txns = []
    alerts = []

    for ticker in MY_PORTFOLIO:
        cik10 = tmap.get(ticker.upper())
        if not cik10:
            print(f"  · {ticker}: нет в реестре SEC (ETF/иностранный/фонд) — пропуск")
            continue
        try:
            filings = recent_form4(cik10)
        except Exception as e:
            print(f"  ✗ {ticker}: submissions error: {e}")
            continue
        print(f"  · {ticker} (CIK {cik10}): {len(filings)} Form 4 за {LOOKBACK_DAYS}д")
        for f in filings:
            if f["accession"] in seen:
                continue
            new_seen.add(f["accession"])
            root = fetch_form4_xml(cik10, f)
            if root is None:
                continue
            parsed = parse_form4(root)
            agg = aggregate(parsed["txns"])
            if not agg:
                continue  # были только грант/опцион/налог — не сигнал убеждённости
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik10)}/"
                   f"{f['acc_nodash']}/{f['accession']}-index.htm")
            record = {
                "ticker": ticker,
                "cik": cik10,
                "insider": parsed["insider"],
                "title": parsed["title"],
                "filing_date": f["filing_date"],
                "accession": f["accession"],
                "url": url,
                "trades": agg,
            }
            fresh_txns.append(record)
            if not seeding:
                alerts.append((ticker, parsed, agg, url))

    # Отправляем алерты (в хронологическом порядке подачи)
    alerts_sent = 0
    if alerts:
        alerts.sort(key=lambda a: a[1]["txns"][0]["date"] if a[1]["txns"] else "")
        for ticker, parsed, agg, url in alerts:
            if notify.send(build_message(ticker, parsed, agg, url)):
                alerts_sent += 1

    # Обновляем состояние
    all_txns = (fresh_txns + kept_txns)[:KEEP_TXNS]

    # Кластеры инсайдеров (несколько топов в одну сторону по одной бумаге).
    clusters = compute_clusters(all_txns)
    cluster_alerts = 0
    new_seen_clusters = set(seen_clusters)
    for c in clusters:
        sig = f"{c['ticker']}|{c['direction']}|{c['count']}"
        new_seen_clusters.add(sig)
        # алертим новый/подросший кластер (сигнатура включает count)
        if not seeding and sig not in seen_clusters:
            if notify.send(build_cluster_alert(c)):
                cluster_alerts += 1

    state = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "portfolio": MY_PORTFOLIO,
        "alert_codes": sorted(ALERT_CODES),
        "seen_accessions": list(new_seen)[:KEEP_SEEN],
        "seen_clusters": list(new_seen_clusters)[:KEEP_SEEN],
        "clusters": clusters,
        "transactions": all_txns,
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    if seeding:
        print(f"✓ Посев завершён: {len(fresh_txns)} подач помечены как виденные, алертов не слали.")
    else:
        print(f"✓ Новых подач: {len(fresh_txns)}, одиночных алертов: {alerts_sent}, "
              f"кластерных: {cluster_alerts} (всего кластеров: {len(clusters)})")


if __name__ == "__main__":
    main()
