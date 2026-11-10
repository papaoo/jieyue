"""Bounded public company-announcement metadata, not inferred document contents.

Schemas follow the installed AKShare stock_notice / stock_disclosure_cninfo
adapters. Requests are bounded to one page and use explicit network timeouts.
"""
from datetime import date, datetime, timedelta, timezone
import re

import requests


def _cninfo(code, start, end):
    response = requests.get("https://www.cninfo.com.cn/new/data/szse_stock.json", timeout=(3, 5))
    response.raise_for_status()
    stock = next((row for row in response.json()["stockList"] if row.get("code") == code), None)
    if not stock:
        return []
    response = requests.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", data={
        "pageNum": "1", "pageSize": "30", "column": "szse", "tabName": "fulltext",
        "stock": f"{code},{stock['orgId']}", "seDate": f"{start}~{end}",
        "isHLtitle": "false", "sortName": "time", "sortType": "desc",
    }, timeout=(3, 5))
    response.raise_for_status()
    payload = response.json()
    if "announcements" not in payload:
        raise ValueError("invalid CNINFO schema")
    records = []
    for row in payload.get("announcements") or []:
        if str(row.get("secCode")) != code:
            continue
        document = str(row.get("adjunctUrl") or "")
        if not re.fullmatch(r"finalpage/[\w/.-]+\.pdf", document, re.I):
            continue
        published = datetime.fromtimestamp(float(row["announcementTime"]) / 1000,
                                           timezone(timedelta(hours=8))).date().isoformat()
        records.append({"title": re.sub(r"<[^>]+>", "", row["announcementTitle"]),
                        "url": "https://static.cninfo.com.cn/" + document,
                        "published_date": published, "issuer_code": code,
                        "source": "CNINFO", "evidence_kind": "announcement_metadata"})
    return records


def _eastmoney(code, start, end):
    response = requests.get("https://np-anotice-stock.eastmoney.com/api/security/ann", params={
        "sr": "-1", "page_size": "100", "page_index": "1", "ann_type": "A",
        "client_source": "web", "f_node": "0", "s_node": "0", "stock_list": code,
        "begin_time": start, "end_time": end,
    }, timeout=(3, 5))
    response.raise_for_status()
    records = []
    for row in response.json()["data"]["list"]:
        issuers = {str(item.get("stock_code")) for item in row.get("codes", [])
                   if str(item.get("ann_type", "")).startswith("A")}
        if issuers != {code}:  # Multi-issuer pages require separate attribution.
            continue
        document_id = str(row.get("art_code") or "")
        if not re.fullmatch(r"[A-Za-z0-9]+", document_id):
            continue
        records.append({"title": row["title"], "issuer_code": code,
                        "published_date": str(row["notice_date"])[:10], "source": "EastmoneyAnnouncement",
                        "url": f"https://data.eastmoney.com/notices/detail/{code}/{document_id}.html",
                        "evidence_kind": "announcement_metadata"})
    return records


def fetch_company_announcements(code: str, *, days: int = 3, limit: int = 10, as_of: date | None = None) -> dict:
    """Return a bounded recent sample; empty/error never means no company risk."""
    now = datetime.now(timezone(timedelta(hours=8)))
    envelope = {"records": [], "source": "", "fetched_at": now.isoformat(), "warnings": [], "ok": False}
    if not re.fullmatch(r"[03689]\d{5}", code):
        envelope["warnings"] = ["unsupported_symbol"]
        return envelope
    anchor = as_of if as_of is not None else now.date()
    if not isinstance(anchor, date) or isinstance(anchor, datetime):
        raise ValueError("announcement_date_invalid")
    start = (anchor - timedelta(days=max(0, min(int(days), 30) - 1))).isoformat()
    end = anchor.isoformat()
    envelope["warnings"].append("有限窗口公告元数据，非全量公告；正文及金额尚未核验，不能仅凭标题推断利好/利空。")
    for source, fetch in (("EastmoneyAnnouncement", _eastmoney), ("CNINFO", _cninfo)):
        try:
            rows = fetch(code, start, end)
            rows = [row for row in rows if start <= row["published_date"] <= end]
            envelope["ok"] = True
            envelope["source"] = source
            if rows:
                seen = set()
                for row in sorted(rows, key=lambda item: item["published_date"], reverse=True):
                    key = (row["title"], row["published_date"])
                    if key not in seen:
                        seen.add(key)
                        envelope["records"].append(row)
                envelope["records"] = envelope["records"][:max(1, min(limit, 30))]
                break
        except Exception as exc:
            # Exception text may include transport/proxy credentials; never expose it.
            envelope["warnings"].append(f"{source}: {type(exc).__name__}")
    envelope["count"] = len(envelope["records"])
    return envelope
