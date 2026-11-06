"""
기사 DB 분석 (DB 분석 창·CLI `stats`·API `/api/archive/stats`가 함께 쓴다)

건수는 DB 행 수 기준: 같은 기사가 온라인과 지면(또는 방송)으로 모두 있으면 각각 1건씩 센다.
구분은 온라인 / 방송 / 지면(그 밖의 "A16면" 같은 값) 세 가지로 묶는다. 기간은 DB의 '날짜'(date) 열 기준.
나중에 보도자료 DB와 잇기 쉽도록, 집계는 행 목록을 받아 숫자만 돌려주는 함수로 둔다.
"""

from collections import defaultdict

KINDS = ("online", "print", "broadcast")
KIND_LABELS = {"online": "온라인", "print": "지면", "broadcast": "방송"}
UNCATEGORIZED = "미분류"


def kind_of(row_type):
    if row_type == "온라인":
        return "online"
    if row_type == "방송":
        return "broadcast"
    return "print"


def empty():
    return {"total": 0, "online": 0, "print": 0, "broadcast": 0}


def summarize(rows, date_from=None, date_to=None):
    """
    rows: 기사 DB 행 (date, press, type, category ...). date_from·date_to: "YYYY-MM-DD" (포함), 비우면 제한 없음
    반환: {"period", "totals", "by_year", "by_month", "by_category", "by_press"}
    by_* 항목: {"key", "total", "online", "print", "broadcast"}
    """
    picked = [
        r for r in rows
        if r.get("date") and (not date_from or r["date"] >= date_from) and (not date_to or r["date"] <= date_to)
    ]
    totals = empty()
    groups = {name: defaultdict(empty) for name in ("year", "month", "category", "press")}
    for r in picked:
        kind = kind_of(r.get("type") or "")
        keys = {
            "year": r["date"][:4],
            "month": r["date"][:7],
            "category": (r.get("category") or "").strip() or UNCATEGORIZED,
            "press": (r.get("press") or "").strip() or "(언론사 없음)",
        }
        for counter in [totals] + [groups[name][key] for name, key in keys.items()]:
            counter["total"] += 1
            counter[kind] += 1

    def listed(name, by_count=False):
        items = [{"key": k} | v for k, v in groups[name].items()]
        if by_count:
            return sorted(items, key=lambda x: (-x["total"], x["key"]))
        return sorted(items, key=lambda x: x["key"])

    dates = sorted(r["date"] for r in picked)
    return {
        "period": {"from": date_from or (dates[0] if dates else None), "to": date_to or (dates[-1] if dates else None),
                   "first_date": dates[0] if dates else None, "last_date": dates[-1] if dates else None},
        "totals": totals,
        "by_year": listed("year"),
        "by_month": listed("month"),
        "by_category": listed("category", by_count=True),
        "by_press": listed("press", by_count=True),
        "kinds": KIND_LABELS,
        "basis": "DB 행 수 기준 (온라인·지면·방송 행을 각각 1건으로 셈), 기간은 '날짜' 열 기준",
    }
