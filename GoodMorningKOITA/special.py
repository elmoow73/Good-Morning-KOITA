"""
특별보도 묶음 (v1.5.0)

기사 DB에서 분류가 [특별보도]인 행을 '특별보도 1건'으로 묶어 기고·인터뷰·기획보도로 나눈다.
기사 DB 열은 그대로 두고, 보도자료처럼 따로 '특별보도 연결표'에 저장한다. 연도는 특별보도 날짜 기준.
특별보도 날짜: 묶음에 지면 기사가 있으면 가장 이른 지면 날짜, 없으면 가장 이른 기사 날짜 (v1.5.0-rc.4).
특별보도 창에서 직접 고칠 수 있고, 고친 날짜(date_set = "1")는 기사를 더하거나 빼도 그대로 둔다:
- koita_special_2026.sqlite : 원본 (한 줄 = 특별보도 1건 ↔ 기사 DB 1행)
- koita_special_2026.csv / .json : 바뀔 때마다 다시 만드는 사본. ARCHIVE_COPY_DIR(드롭박스)에도 복사
기사 하나(DB 1행)는 특별보도 하나에만 묶인다.

건수: 특별보도는 묶음 1개 = 1건 (온라인+지면이어도, 기사가 여러 꼭지로 나뉘어 나가도 1건).
묶음 안의 기사 행 수(온라인·지면·방송)는 따로 보여 준다.

자동 묶음 제안: 아직 묶지 않은 [특별보도] 행을 '같은 언론사 + 같은 날짜'끼리 묶는다.
지면 행의 날짜는 온라인 날짜 + 1일(엑셀 규칙)이므로 지면 행은 하루 앞 날짜로 맞춰 비교한다.
예) 온라인 2건(11-09) + 지면 2건(11-10)이 같은 언론사면 한 묶음 (4행 → 특별보도 1건)
"""

import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta

import archive
import pressrelease
import printcheck
import stats

CATEGORY = "특별보도"
SUBTYPES = ["기고", "인터뷰", "기획보도"]
DB_PREFIX = "koita_special"

COLUMNS = [
    ("seq", "연번"),
    ("group_id", "특별보도ID"),
    ("group_date", "특별보도날짜"),
    ("date_set", "날짜직접수정"),
    ("subtype", "구분"),
    ("group_title", "특별보도명"),
    ("article_date", "기사날짜"),
    ("press", "언론사명"),
    ("type", "기사구분"),
    ("title", "기사제목"),
    ("url", "URL"),
    ("article_key", "기사키"),
    ("linked_at", "연결일시"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       TEXT NOT NULL,
    group_date     TEXT NOT NULL,
    date_set       TEXT NOT NULL DEFAULT '',
    subtype        TEXT NOT NULL,
    group_title    TEXT NOT NULL,
    article_date   TEXT NOT NULL,
    press          TEXT NOT NULL,
    type           TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    article_key    TEXT NOT NULL UNIQUE,
    linked_at      TEXT NOT NULL
);
"""

# 제목으로 구분을 짐작할 때 (제안만, 사용자가 바꿀 수 있음)
GUESS_WORDS = {
    "기고": ["기고", "단상", "칼럼", "시론", "기고문", "포럼", "논단", "시평", "광장", "발언대", "열린세상", "아침을 열며"],
    "인터뷰": ["인터뷰", "만나다", "만난 사람", "대담", "일문일답", "Q&A"],
}


class SpecialError(Exception):
    pass


# ---------------- 연결표 (연도별 sqlite·csv·json) ----------------

def paths(year):
    base = archive.ARCHIVE_DIR / f"{DB_PREFIX}_{year}"
    return {"sqlite": base.with_suffix(".sqlite"), "csv": base.with_suffix(".csv"), "json": base.with_suffix(".json")}


def years():
    return sorted({int(m.group(1)) for p in archive.ARCHIVE_DIR.glob(f"{DB_PREFIX}_*.sqlite")
                   if (m := re.fullmatch(rf"{DB_PREFIX}_(\d{{4}})\.sqlite", p.name))})


def year_of(d):
    m = re.match(r"(\d{4})", d or "")
    return int(m.group(1)) if m else archive.this_year()


@contextmanager
def connect(year):
    archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths(year)["sqlite"])
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        if "date_set" not in {r[1] for r in conn.execute("PRAGMA table_info(links)")}:  # rc.3에서 만든 연결표
            conn.execute("ALTER TABLE links ADD COLUMN date_set TEXT NOT NULL DEFAULT ''")
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_links(year=None):
    names = ", ".join(n for n, _ in COLUMNS)
    out = []
    for y in ([year] if year else years()):
        if not paths(y)["sqlite"].exists():
            continue
        with connect(y) as conn:
            out += [dict(r) for r in conn.execute(f"SELECT {names} FROM links ORDER BY seq")]
    return out


def write_year(year, links):
    links = sorted(links, key=lambda l: (l["article_date"], l["press"], l["type"], l["title"]))
    links.sort(key=lambda l: (l["group_date"], l["group_id"]), reverse=True)
    names = [n for n, _ in COLUMNS if n != "seq"]
    with connect(year) as conn:
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'links'")
        conn.executemany(
            f"INSERT INTO links (seq, {', '.join(names)}) VALUES ({', '.join('?' * (len(names) + 1))})",
            [(seq, *(l[n] for n in names)) for seq, l in enumerate(links, start=1)],
        )
    return archive.write_table_copies(paths(year), COLUMNS, load_links(year))


def plain(links):
    return [{k: v for k, v in l.items() if k != "article"} for l in links]


def auto_date(links):
    """특별보도 날짜 기본값: 지면 기사가 있으면 가장 이른 지면 날짜, 없으면 가장 이른 기사 날짜"""
    prints = [l["article_date"] for l in links if archive.is_print(l["type"])]
    return min(prints or [l["article_date"] for l in links])


def save_all(links, touched_years=()):
    """
    links(모든 연도)를 특별보도 날짜의 연도별로 나눠 저장한다.
    직접 고친 날짜(date_set = "1")는 그대로, 아니면 auto_date로 맞춘다
    """
    by_group = {}
    for l in links:
        l.setdefault("date_set", "")
        by_group.setdefault(l["group_id"], []).append(l)
    for members in by_group.values():
        fixed = next((l for l in members if l["date_set"] == "1"), None)
        day = fixed["group_date"] if fixed else auto_date(members)
        for l in members:
            l["group_date"], l["date_set"] = day, "1" if fixed else ""
    by_year, before = {}, {}
    for l in links:
        by_year.setdefault(year_of(l["group_date"]), []).append(l)
    for l in load_links():
        before.setdefault(year_of(l["group_date"]), []).append(l)
    key = lambda rows: sorted(tuple(sorted((k, v) for k, v in r.items() if k != "seq")) for r in rows)
    warnings = []
    for year in sorted(set(by_year) | set(before) | set(touched_years)):
        if year in touched_years or key(by_year.get(year, [])) != key(before.get(year, [])):
            warnings += write_year(year, by_year.get(year, []))
    return warnings


def heal(rows):
    """기사 DB가 바뀌어 연결표의 키·기사 정보가 달라졌으면 고쳐 저장한다 (보도자료 연결표와 같은 방식)"""
    links, changed = pressrelease.resolve(load_links(), rows)
    warnings = save_all(plain(links)) if changed else []
    return links, warnings


# ---------------- 자동 묶음 제안 ----------------

def base_date(row):
    """같은 기사의 온라인·지면을 같은 날로 맞춘 날짜 (지면 행 = 날짜 - 1일)"""
    try:
        d = date.fromisoformat(row["date"])
    except (TypeError, ValueError):
        return row.get("date") or ""
    if archive.is_print(row["type"]):
        d -= timedelta(days=1)
    return d.isoformat()


def guess_subtype(titles):
    text = " ".join(titles)
    for subtype, words in GUESS_WORDS.items():
        if any(w.lower() in text.lower() for w in words):
            return subtype
    return "기획보도"


def default_title(articles):
    """묶음 이름 기본값: 온라인 기사 중 가장 먼저 나온 제목 (없으면 첫 기사 제목)"""
    online = [a for a in articles if a["kind"] == "online"]
    return (online or articles)[0]["title"] if articles else ""


def proposals(rows, linked_keys, date_from=None, date_to=None):
    """아직 묶지 않은 [특별보도] 행을 같은 언론사 + 같은 날짜(지면은 하루 앞)로 묶은 제안 목록"""
    groups = {}
    for r in rows:
        if (r.get("category") or "") != CATEGORY or r["row_key"] in linked_keys:
            continue
        if not in_period(r["date"], date_from, date_to):
            continue
        key = (printcheck.norm(r["press"]), base_date(r))
        groups.setdefault(key, []).append(pressrelease.article_view(r))
    out = []
    for (_, day), articles in groups.items():
        articles.sort(key=lambda a: (a["date"], {"online": 0, "broadcast": 1, "print": 2}[a["kind"]], a["title"]))
        prints = [a["date"] for a in articles if a["kind"] == "print"]
        out.append({"key": f"{articles[0]['press']}|{day}", "date": min(prints) if prints else day,
                    "press": articles[0]["press"],
                    "articles": articles, "counts": pressrelease.counts(articles),
                    "subtype": guess_subtype([a["title"] for a in articles]), "title": default_title(articles)})
    out.sort(key=lambda g: (g["date"], g["press"]), reverse=True)
    return out


def in_period(d, date_from, date_to):
    return bool(d) and (not date_from or d >= date_from) and (not date_to or d <= date_to)


# ---------------- 보기 ----------------

def overview(rows=None, date_from=None, date_to=None):
    """
    등록한 특별보도(묶음)와 묶음 제안. date_from·date_to: 특별보도 날짜(묶음의 첫 기사 날짜) 기간, 제안은 기사 날짜 기간
    """
    rows = archive.all_rows() if rows is None else rows
    for r in rows:
        r.setdefault("row_key", archive.make_row_key(r))
    links, warnings = heal(rows)
    groups = {}
    for link in links:
        g = groups.setdefault(link["group_id"], {"id": link["group_id"], "date": link["group_date"],
                                                 "date_set": link.get("date_set") == "1",
                                                 "subtype": link["subtype"], "title": link["group_title"],
                                                 "articles": [], "missing_articles": []})
        if link["article"] is None:
            g["missing_articles"].append({k: link[k] for k in ("article_date", "press", "type", "title", "url", "article_key")})
        else:
            g["articles"].append(pressrelease.article_view(link["article"]))
    items = [g for g in groups.values() if in_period(g["date"], date_from, date_to) or not (date_from or date_to)]
    for g in items:
        g["articles"].sort(key=lambda a: (a["date"], {"online": 0, "broadcast": 1, "print": 2}[a["kind"]], a["title"]))
        g["counts"] = pressrelease.counts(g["articles"])
        g["press"] = ", ".join(dict.fromkeys(a["press"] for a in g["articles"])) or \
            ", ".join(dict.fromkeys(a["press"] for a in g["missing_articles"]))
        g["not_special"] = sum(1 for a in g["articles"] if a["category"] != CATEGORY)
    items.sort(key=lambda g: (g["date"], g["title"]), reverse=True)
    linked_keys = {l["article_key"] for l in links if l["article"] is not None}
    props = proposals(rows, linked_keys, date_from, date_to)
    return {
        "groups": items,
        "proposals": props,
        "summary": summary(items, sum(len(p["articles"]) for p in props)),
        "subtypes": SUBTYPES,
        "period": {"from": date_from, "to": date_to},
        "warnings": warnings,
        "basis": "특별보도는 묶음 1개 = 1건 (온라인·지면, 여러 꼭지로 나뉜 기사도 1건). 기간은 특별보도 날짜(묶음의 첫 기사 날짜) 기준",
    }


def summary(items, unassigned):
    by_subtype = {s: {"count": 0, "rows": stats.empty()} for s in SUBTYPES}
    for g in items:
        slot = by_subtype.setdefault(g["subtype"], {"count": 0, "rows": stats.empty()})
        slot["count"] += 1
        for k, v in g["counts"].items():
            slot["rows"][k] += v
    rows_total = pressrelease.counts([a for g in items for a in g["articles"]])
    return {"total": len(items), "rows": rows_total, "by_subtype": by_subtype, "unassigned": unassigned,
            "missing_articles": sum(len(g["missing_articles"]) for g in items)}


def stats_section(date_from=None, date_to=None, rows=None):
    """DB 분석에 넣을 특별보도 건수와 목록"""
    data = overview(rows=rows, date_from=date_from, date_to=date_to)
    return {
        "summary": data["summary"],
        "subtypes": SUBTYPES,
        "groups": [{"id": g["id"], "date": g["date"], "subtype": g["subtype"], "title": g["title"], "press": g["press"]}
                   | g["counts"] for g in data["groups"]],
    }


# ---------------- 묶기·고치기·풀기 ----------------

def new_group_id():
    return f"sp_{datetime.now():%Y%m%d%H%M%S}_{secrets.token_hex(3)}"


def check_subtype(subtype):
    if subtype not in SUBTYPES:
        raise SpecialError(f"구분은 {'·'.join(SUBTYPES)} 중 하나여야 합니다.")


def check_date(day):
    """직접 넣은 날짜 (YYYY-MM-DD). 비우면 None (자동)"""
    day = (day or "").strip()
    if not day:
        return None
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        raise SpecialError("날짜는 2026-11-09 형식이어야 합니다.")


def group(keys, subtype=None, title=None, group_id=None, rows=None, day=None):
    """
    기사 DB 행들(keys = row_key)을 특별보도 1건으로 묶는다. group_id를 주면 그 특별보도에 더한다(구분·이름·날짜는 그대로).
    day: 특별보도 날짜를 직접 정할 때 (비우면 지면 기사 날짜 → 없으면 첫 기사 날짜)
    다른 특별보도에 이미 묶인 행이 있으면 SpecialError. 반환: {"group_id", "linked", "already", "warnings"}
    """
    rows = archive.all_rows() if rows is None else rows
    day = check_date(day)
    keys = list(dict.fromkeys(keys or []))
    if not keys:
        raise SpecialError("묶을 기사를 고르세요.")
    links, _ = pressrelease.resolve(load_links(), rows)
    by_key = pressrelease.article_index(rows)[0]
    if group_id:
        existing = [l for l in links if l["group_id"] == group_id]
        if not existing:
            raise SpecialError("더할 특별보도를 찾을 수 없습니다. 새로 고침한 뒤 다시 하세요.")
        subtype, title = existing[0]["subtype"], existing[0]["group_title"]
    else:
        check_subtype(subtype)
        title = re.sub(r"\s+", " ", title or "").strip()
        group_id = new_group_id()
    owner = {l["article_key"]: l for l in links}
    new, already, problems = [], 0, []
    for key in keys:
        row = by_key.get(key)
        if row is None:
            problems.append(f"기사 DB에 없는 행입니다: {key}")
        elif key in owner and owner[key]["group_id"] != group_id:
            problems.append(f"'{row['title']}'({row['press']} {row['type']})은 이미 "
                            f"'{owner[key]['group_title']}'에 묶여 있습니다. 그쪽에서 먼저 빼세요.")
        elif key in owner:
            already += 1
        else:
            new.append(row)
    if problems:
        raise SpecialError(" / ".join(problems))
    if not title:
        title = default_title([pressrelease.article_view(r) for r in new]) if new else ""
    linked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    fixed = {"group_date": day, "date_set": "1"} if day and not existing_group(links, group_id) else {}
    out = plain(links) + [{"group_id": group_id, "group_date": r["date"], "date_set": "", "subtype": subtype,
                           "group_title": title, "linked_at": linked_at} | pressrelease.snapshot(r) | fixed for r in new]
    touched = {year_of(l["group_date"]) for l in out if l["group_id"] == group_id}
    warnings = save_all(out, touched_years=touched if new else ())
    return {"group_id": group_id, "linked": len(new), "already": already, "warnings": warnings}


def existing_group(links, group_id):
    return any(l["group_id"] == group_id for l in links)


def update(group_id, subtype=None, title=None, rows=None, day=None):
    """
    특별보도 1건의 구분·이름·날짜를 바꾼다.
    day: None = 그대로, "" = 자동(지면 기사 날짜 → 없으면 첫 기사 날짜)으로 되돌림, "2026-11-10" = 직접 정함
    """
    rows = archive.all_rows() if rows is None else rows
    reset = day is not None and not day.strip()
    day = check_date(day)
    if subtype is not None:
        check_subtype(subtype)
    title = re.sub(r"\s+", " ", title).strip() if title is not None else None
    if title is not None and not title:
        raise SpecialError("특별보도 이름을 넣으세요.")
    links, _ = pressrelease.resolve(load_links(), rows)
    out = plain(links)
    found = [l for l in out if l["group_id"] == group_id]
    if not found:
        raise SpecialError("특별보도를 찾을 수 없습니다. 새로 고침한 뒤 다시 하세요.")
    for l in found:
        if subtype is not None:
            l["subtype"] = subtype
        if title is not None:
            l["group_title"] = title
        if day:
            l["group_date"], l["date_set"] = day, "1"
        elif reset:
            l["date_set"] = ""
    old_year = year_of(found[0]["group_date"])
    warnings = save_all(out, touched_years={old_year, year_of(found[0]["group_date"])})
    return {"updated": len(found), "warnings": warnings}


def unlink(keys=None, group_id=None, rows=None):
    """묶음에서 기사를 뺀다 (keys = 기사키). group_id를 주면 그 특별보도를 통째로 푼다"""
    rows = archive.all_rows() if rows is None else rows
    keys = set(keys or [])
    if not keys and not group_id:
        raise SpecialError("뺄 기사를 고르세요.")
    links, _ = pressrelease.resolve(load_links(), rows)
    out = plain(links)
    gone = [l for l in out if l["article_key"] in keys or (group_id and l["group_id"] == group_id)]
    kept = [l for l in out if l not in gone]
    warnings = save_all(kept, touched_years={year_of(l["group_date"]) for l in gone})
    return {"unlinked": len(gone), "warnings": warnings}
