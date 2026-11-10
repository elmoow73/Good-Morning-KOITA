"""
보도자료 ↔ 기사 연결 (v1.5.0)

보도자료 목록은 보도자료 폴더(.env PRESSRELEASE_DIR)의 파일 이름에서 바로 읽는다. 따로 등록할 필요가 없다.
파일명 형식: 배포일_[종류] 보도자료명.확장자   (예: 2026-11-09_[보도자료] 2026년 하반기 탄소중립 K-Tech 포럼 개최.hwpx)
- 배포일은 2026-11-09, 20261109, 2026.11.09 등을 읽는다. [종류]는 없어도 된다 (보도자료·보도참고자료 등)
- 배포일과 보도자료명이 같은 파일(hwpx·pdf 등)은 한 건으로 묶는다 (띄어쓰기·기호 차이는 무시)

연결은 기사 DB와 따로 두는 '연결표'에 저장한다 (기사 DB 열은 그대로). 배포일 연도별 파일:
- koita_pressrelease_2026.sqlite : 원본 (한 줄 = 보도자료 1건 ↔ 기사 DB 1행)
- koita_pressrelease_2026.csv / .json : 바뀔 때마다 다시 만드는 사본. ARCHIVE_COPY_DIR(드롭박스)에도 복사
기사 하나(DB 1행)는 보도자료 하나에만 연결된다. 건수는 기사 DB 분석과 같이 행 기준 (온라인·지면·방송 각각 1건).

기사 행은 row_key(온라인·방송 = URL+구분, 지면 = 날짜+언론사+구분+제목)로 가리킨다. 편집기에서 지면 행의
날짜·제목을 고쳐 키가 바뀌면, 연결표에 같이 적어 둔 언론사·제목·URL로 다시 찾아 키를 고쳐 둔다.
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import archive
import printcheck
import stats

FILE_EXTENSIONS = {".hwpx", ".hwp", ".pdf", ".docx", ".doc", ".txt", ".odt", ".rtf"}
DB_PREFIX = "koita_pressrelease"
CATEGORY = "보도자료"  # 연결 후보로 보여 줄 기사 DB 분류
SKIP_DIRS = {"_이전", printcheck.TRASH_DIR}
# 연결 후보: 기사 날짜가 배포일 하루 전 ~ 7일 뒤. 제목 낱말이 이만큼 겹치면 '추천'
CANDIDATE_BEFORE_DAYS = 1
CANDIDATE_AFTER_DAYS = 7
RECOMMEND_SCORE = 0.3

# (열 이름, CSV 머리글)
COLUMNS = [
    ("seq", "연번"),
    ("release_id", "보도자료ID"),
    ("release_date", "배포일"),
    ("release_kind", "종류"),
    ("release_title", "보도자료명"),
    ("article_date", "기사날짜"),
    ("press", "언론사명"),
    ("type", "구분"),
    ("title", "기사제목"),
    ("url", "URL"),
    ("article_key", "기사키"),
    ("linked_at", "연결일시"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id     TEXT NOT NULL,
    release_date   TEXT NOT NULL,
    release_kind   TEXT NOT NULL DEFAULT '',
    release_title  TEXT NOT NULL,
    article_date   TEXT NOT NULL,
    press          TEXT NOT NULL,
    type           TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    article_key    TEXT NOT NULL UNIQUE,
    linked_at      TEXT NOT NULL
);
"""


class LinkError(Exception):
    pass


# ---------------- 보도자료 목록 (폴더의 파일 이름) ----------------

def default_folder():
    return os.getenv("PRESSRELEASE_DIR", "").strip()


def clean_title(text):
    return re.sub(r"\s+", " ", text or "").strip(" _-")


def release_id(release_date, title):
    """보도자료 ID: 배포일 + 보도자료명 (띄어쓰기·기호 무시). 같은 ID의 파일은 한 건으로 묶는다"""
    return f"{release_date}_{printcheck.norm(title)}"


DATE_HEAD_RE = re.compile(r"^\s*(\d{4}[-.]?\d{1,2}[-.]?\d{1,2}|\d{6})\s*[_ ]\s*(.*)$")


def parse_filename(name):
    """
    파일명 1개를 읽는다.
    반환: {"file", "date", "kind", "title", "id", "errors": [메시지]}
    """
    stem = Path(name).stem
    result = {"file": name, "date": None, "kind": "", "title": "", "id": None, "errors": []}
    m = DATE_HEAD_RE.match(stem)
    found = printcheck.parse_date(m.group(1)) if m else None
    if not found:
        result["title"] = clean_title(stem)
        result["errors"].append("맨 앞에서 배포일을 읽지 못했습니다. '2026-11-09_[보도자료] 보도자료명' 형식으로 넣어 주세요.")
        return result
    rest = m.group(2)
    bracket = printcheck.BRACKET_RE.match(rest)
    if bracket:
        result["kind"], rest = bracket.group(1).strip(), bracket.group(2)
    result["date"] = found.isoformat()
    result["title"] = clean_title(rest)
    if not printcheck.norm(result["title"]):
        result["errors"].append("보도자료명이 비어 있습니다.")
        return result
    result["id"] = release_id(result["date"], result["title"])
    return result


def list_files(folder):
    """보도자료 폴더(하위 폴더 포함)의 파일 이름 (폴더 안 상대 경로). 폴더가 없으면 FileNotFoundError"""
    root = Path(folder)
    if not root.is_dir():
        raise FileNotFoundError(f"보도자료 폴더를 찾을 수 없습니다: {folder}")
    names = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if path.is_file() and path.suffix.lower() in FILE_EXTENSIONS and not path.name.startswith(("~$", ".")):
            names.append(rel.as_posix())
    return sorted(names)


def file_path(folder, name):
    """보도자료 폴더 안의 파일 경로. 폴더 밖을 가리키면 FileNotFoundError"""
    root = Path(folder).resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        raise FileNotFoundError(f"보도자료 파일을 찾을 수 없습니다: {name}")
    return path


def releases_from_files(names):
    """파일 이름들 → (보도자료 목록, 읽지 못한 파일). 같은 배포일·보도자료명의 파일은 한 건으로"""
    by_id = {}
    errors = []
    for name in names:
        info = parse_filename(name)
        if not info["id"]:
            errors.append({"file": name, "message": " ".join(info["errors"])})
            continue
        item = by_id.setdefault(info["id"], {"id": info["id"], "date": info["date"], "kind": info["kind"],
                                             "title": info["title"], "files": []})
        item["files"].append(name)
        if not item["kind"] and info["kind"]:
            item["kind"] = info["kind"]
    return list(by_id.values()), errors


# ---------------- 연결표 (연도별 sqlite·csv·json) ----------------

def paths(year):
    base = archive.ARCHIVE_DIR / f"{DB_PREFIX}_{year}"
    return {"sqlite": base.with_suffix(".sqlite"), "csv": base.with_suffix(".csv"), "json": base.with_suffix(".json")}


def years():
    """연결표가 있는 연도들 (오래된 순)"""
    return sorted({int(m.group(1)) for p in archive.ARCHIVE_DIR.glob(f"{DB_PREFIX}_*.sqlite")
                   if (m := re.fullmatch(rf"{DB_PREFIX}_(\d{{4}})\.sqlite", p.name))})


def year_of_release(release_date):
    m = re.match(r"(\d{4})", release_date or "")
    return int(m.group(1)) if m else archive.this_year()


@contextmanager
def connect(year):
    archive.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths(year)["sqlite"])
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def load_links(year=None):
    """연결표 행. year가 None이면 모든 연도"""
    names = ", ".join(n for n, _ in COLUMNS)
    out = []
    for y in ([year] if year else years()):
        if not paths(y)["sqlite"].exists():
            continue
        with connect(y) as conn:
            out += [dict(r) for r in conn.execute(f"SELECT {names} FROM links ORDER BY seq")]
    return out


def sort_key(link):
    """배포일 최신순 → 보도자료명 → 기사 날짜순 → 언론사"""
    return (link["release_date"], link["release_title"], link["release_id"])


def write_year(year, links):
    """year 연도 연결표를 links로 통째로 바꾸고 CSV·JSON 사본·드롭박스 복사본도 다시 만든다. 반환: 안내 문구"""
    links = sorted(links, key=lambda l: (l["article_date"], l["press"], l["type"], l["title"]))
    links.sort(key=sort_key, reverse=True)
    names = [n for n, _ in COLUMNS if n != "seq"]
    with connect(year) as conn:
        conn.execute("DELETE FROM links")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'links'")
        conn.executemany(
            f"INSERT INTO links (seq, {', '.join(names)}) VALUES ({', '.join('?' * (len(names) + 1))})",
            [(seq, *(l[n] for n in names)) for seq, l in enumerate(links, start=1)],
        )
    return archive.write_table_copies(paths(year), COLUMNS, load_links(year))


def save_all(links, touched_years=()):
    """links(모든 연도)를 배포일 연도별로 나눠 저장한다. 바뀐 연도와 touched_years만 다시 쓴다"""
    by_year = {}
    for l in links:
        by_year.setdefault(year_of_release(l["release_date"]), []).append(l)
    before = {}
    for l in load_links():
        before.setdefault(year_of_release(l["release_date"]), []).append(l)
    warnings = []
    plain = lambda rows: sorted(tuple(sorted((k, v) for k, v in r.items() if k != "seq")) for r in rows)
    for year in sorted(set(by_year) | set(before) | set(touched_years)):
        if year in touched_years or plain(by_year.get(year, [])) != plain(before.get(year, [])):
            warnings += write_year(year, by_year.get(year, []))
    return warnings


# ---------------- 기사 DB 행 찾기 ----------------

def article_index(rows):
    """기사 DB 행을 여러 방법으로 찾을 수 있게 색인한다"""
    by_key, by_url, by_print = {}, {}, {}
    for r in rows:
        r.setdefault("row_key", archive.make_row_key(r))
        by_key[r["row_key"]] = r
        if archive.is_print(r["type"]):
            by_print.setdefault((printcheck.norm(r["press"]), printcheck.norm(r["title"])), []).append(r)
        else:
            by_url[(r["url"], r["type"])] = r
    return by_key, by_url, by_print


def find_article(link, index, aliases):
    """연결표 행이 가리키는 지금의 기사 DB 행. 키가 바뀌었으면 URL·언론사·제목으로 다시 찾는다. 없으면 None"""
    by_key, by_url, by_print = index
    if link["article_key"] in by_key:
        return by_key[link["article_key"]]
    if not archive.is_print(link["type"]):
        return by_url.get((link["url"], link["type"]))
    titles = [link["title"]]
    for _ in range(3):  # 지면 확인에서 제목을 바꾼 기록 (여러 번 바꾼 경우까지)
        titles += [a["new"] for a in aliases if a["press"] == link["press"] and a["old"] in titles and a["new"] not in titles]
    for title in titles:
        found = by_print.get((printcheck.norm(link["press"]), printcheck.norm(title)), [])
        if found:
            # 같은 언론사·제목의 지면 행이 여럿이면 날짜가 가까운 것
            return min(found, key=lambda r: printcheck.day_gap(r["date"], link["article_date"]) or 0)
    # 편집기에서 제목만 고친 경우: 같은 언론사·날짜·구분의 지면 행이 딱 하나면 그 행
    same = [r for rs in by_print.values() for r in rs
            if r["date"] == link["article_date"] and r["type"] == link["type"]
            and printcheck.norm(r["press"]) == printcheck.norm(link["press"])]
    return same[0] if len(same) == 1 else None


def snapshot(row):
    """연결표에 같이 적어 두는 기사 정보"""
    return {"article_date": row["date"], "press": row["press"], "type": row["type"], "title": row["title"],
            "url": row["url"], "article_key": row["row_key"]}


def resolve(links, rows):
    """
    연결표 행마다 지금 기사 DB 행을 붙인다 (link["article"], 못 찾으면 None).
    키·기사 정보가 바뀐 행은 연결표 값을 새로 고친다. 반환: (links, 고친 것이 있는지)
    """
    index = article_index(rows)
    aliases = archive.load_title_aliases()
    changed = False
    seen = set()
    out = []
    for link in links:
        row = find_article(link, index, aliases)
        if row is not None:
            if row["row_key"] in seen:  # 두 연결이 같은 행을 가리키게 됐으면 먼저 것만
                changed = True
                continue
            seen.add(row["row_key"])
            fresh = snapshot(row)
            if any(link[k] != v for k, v in fresh.items()):
                link.update(fresh)
                changed = True
        out.append(link | {"article": row})
    return out, changed


def heal(rows=None):
    """기사 DB가 바뀌어 연결표의 키·기사 정보가 달라졌으면 연결표를 고쳐 저장한다. 반환: (연결, 안내 문구)"""
    rows = archive.all_rows() if rows is None else rows
    links, changed = resolve(load_links(), rows)
    warnings = []
    if changed:
        warnings = save_all([{k: v for k, v in l.items() if k != "article"} for l in links])
    return links, warnings


# ---------------- 보도자료별 건수 ----------------

def counts(articles):
    c = stats.empty()
    for a in articles:
        c["total"] += 1
        c[stats.kind_of(a["type"])] += 1
    return c


def overview(folder=None, names=None, rows=None, date_from=None, date_to=None):
    """
    보도자료 목록과 보도자료별 연결 기사·건수.
    folder: 보도자료 폴더 (비우면 .env PRESSRELEASE_DIR). names: 폴더 대신 파일 이름 목록 (폰·클라우드에서 고른 폴더)
    date_from·date_to: 배포일 기간 (YYYY-MM-DD, 포함). 미연결 기사는 기사 날짜가 이 기간인 것
    """
    rows = archive.all_rows() if rows is None else rows
    folder = (folder or "").strip() or default_folder()
    source, folder_error = None, None
    if names is not None:
        source = "선택한 폴더"
    elif folder:
        try:
            names, source = list_files(folder), folder
        except FileNotFoundError as exc:
            folder_error = str(exc)
    else:
        folder_error = "보도자료 폴더가 정해지지 않았습니다 (.env의 PRESSRELEASE_DIR)."
    releases, file_errors = releases_from_files(names or [])
    links, warnings = heal(rows)

    by_id = {r["id"]: r | {"articles": [], "missing_articles": [], "has_file": True} for r in releases}
    for link in links:
        item = by_id.get(link["release_id"])
        if item is None:  # 파일이 없어진(또는 폴더를 못 읽은) 보도자료도 연결표에 있으면 보여 준다
            item = by_id[link["release_id"]] = {"id": link["release_id"], "date": link["release_date"],
                                                "kind": link["release_kind"], "title": link["release_title"],
                                                "files": [], "articles": [], "missing_articles": [], "has_file": False}
        art = link["article"]
        if art is None:
            item["missing_articles"].append({k: link[k] for k in ("article_date", "press", "type", "title", "url", "article_key")})
        else:
            item["articles"].append(article_view(art))

    def in_period(d):
        return bool(d) and (not date_from or d >= date_from) and (not date_to or d <= date_to)

    items = [r for r in by_id.values() if in_period(r["date"]) or not (date_from or date_to)]
    for r in items:
        r["articles"].sort(key=lambda a: (a["date"], a["press"], a["type"]))
        r["counts"] = counts(r["articles"])
        r["not_press_release"] = sum(1 for a in r["articles"] if a["category"] != CATEGORY)
    items.sort(key=lambda r: (r["date"], r["title"]), reverse=True)

    linked_keys = {l["article_key"] for l in links if l["article"] is not None}
    unlinked = [article_view(r) for r in rows
                if (r.get("category") or "") == CATEGORY and r["row_key"] not in linked_keys
                and (in_period(r["date"]) or not (date_from or date_to))]
    unlinked.sort(key=lambda a: (a["date"], a["press"]), reverse=True)

    total = counts([a for r in items for a in r["articles"]])
    with_files = [r for r in items if r["has_file"]]
    return {
        "releases": items,
        "unlinked": unlinked,
        "summary": {
            "releases": len(items),
            "releases_with_articles": sum(1 for r in items if r["counts"]["total"]),
            "releases_without_articles": sum(1 for r in items if not r["counts"]["total"]),
            "releases_without_file": len(items) - len(with_files),
            "linked": total,
            "average": round(total["total"] / len(items), 1) if items else 0,
            "unlinked": len(unlinked),
            "missing_articles": sum(len(r["missing_articles"]) for r in items),
        },
        "file_errors": file_errors,
        "source": source,
        "folder_error": folder_error,
        "period": {"from": date_from, "to": date_to},
        "warnings": warnings,
        "basis": "DB 행 수 기준 (온라인·지면·방송 행을 각각 1건으로 셈). 보도자료 기간은 배포일, 미연결 기사 기간은 기사 '날짜' 열 기준",
    }


def article_view(row):
    return {"key": row["row_key"], "date": row["date"], "press": row["press"], "type": row["type"],
            "kind": stats.kind_of(row["type"]), "title": row["title"], "url": row["url"],
            "category": row.get("category") or "", "published_at": row.get("published_at") or ""}


def candidates(release, rows=None, links=None):
    """
    보도자료 1건의 연결 후보: 분류가 '보도자료'이고 아직 어느 보도자료에도 연결되지 않은 기사 DB 행.
    기사 날짜가 배포일 하루 전 ~ 7일 뒤인 것(near)을 제목이 비슷한 순으로. 그 밖의 미연결 기사는 others에
    release: {"date", "title"}
    """
    rows = archive.all_rows() if rows is None else rows
    if links is None:
        links, _ = heal(rows)
    linked = {l["article_key"] for l in links if l.get("article") is not None or "article" not in l}
    start = date.fromisoformat(release["date"]) - timedelta(days=CANDIDATE_BEFORE_DAYS)
    end = date.fromisoformat(release["date"]) + timedelta(days=CANDIDATE_AFTER_DAYS)
    near, others = [], []
    for r in rows:
        r.setdefault("row_key", archive.make_row_key(r))
        if (r.get("category") or "") != CATEGORY or r["row_key"] in linked:
            continue
        view = article_view(r)
        view["score"] = round(printcheck.keyword_overlap(release["title"], r["title"]), 2)
        view["shared_words"] = printcheck.shared_words(release["title"], r["title"])
        try:
            d = date.fromisoformat(r["date"])
        except ValueError:
            d = None
        view["recommended"] = view["score"] >= RECOMMEND_SCORE
        (near if d and start <= d <= end else others).append(view)
    near.sort(key=lambda a: (-a["score"], a["date"], a["press"]))
    others.sort(key=lambda a: (a["date"], a["press"]), reverse=True)
    return {"near": near, "others": others,
            "window": {"from": start.isoformat(), "to": end.isoformat()}}


def link(release, keys, rows=None):
    """
    기사 DB 행들(keys = row_key)을 보도자료 1건에 연결한다.
    release: {"id", "date", "kind", "title"}. 다른 보도자료에 이미 연결된 행이 있으면 LinkError (먼저 연결을 끊어야 함)
    반환: {"linked", "already", "warnings"}
    """
    rows = archive.all_rows() if rows is None else rows
    if not keys:
        raise LinkError("연결할 기사를 고르세요.")
    if not (release.get("date") and printcheck.norm(release.get("title"))):
        raise LinkError("보도자료의 배포일·보도자료명이 필요합니다.")
    rid = release.get("id") or release_id(release["date"], release["title"])
    links, _ = resolve(load_links(), rows)
    by_key = article_index(rows)[0]
    owner = {l["article_key"]: l for l in links}
    new, already, problems = [], 0, []
    for key in dict.fromkeys(keys):
        row = by_key.get(key)
        if row is None:
            problems.append(f"기사 DB에 없는 행입니다: {key}")
        elif key in owner and owner[key]["release_id"] != rid:
            other = owner[key]
            problems.append(f"'{row['title']}'({row['press']} {row['type']})은 이미 "
                            f"'{other['release_date']} {other['release_title']}'에 연결돼 있습니다. 그쪽 연결을 먼저 끊으세요.")
        elif key in owner:
            already += 1
        else:
            new.append(row)
    if problems:
        raise LinkError(" / ".join(problems))
    linked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    plain = [{k: v for k, v in l.items() if k != "article"} for l in links]
    plain += [{"release_id": rid, "release_date": release["date"], "release_kind": release.get("kind") or "",
               "release_title": clean_title(release["title"]), "linked_at": linked_at} | snapshot(r) for r in new]
    warnings = save_all(plain, touched_years={year_of_release(release["date"])} if new else ())
    return {"linked": len(new), "already": already, "warnings": warnings}


def unlink(keys, rows=None):
    """연결을 끊는다 (keys = 연결표의 기사키). 반환: {"unlinked", "warnings"}"""
    rows = archive.all_rows() if rows is None else rows
    keys = set(keys or [])
    if not keys:
        raise LinkError("연결을 끊을 기사를 고르세요.")
    links, _ = resolve(load_links(), rows)
    plain = [{k: v for k, v in l.items() if k != "article"} for l in links]
    kept = [l for l in plain if l["article_key"] not in keys]
    touched = {year_of_release(l["release_date"]) for l in plain if l["article_key"] in keys}
    warnings = save_all(kept, touched_years=touched)
    return {"unlinked": len(plain) - len(kept), "warnings": warnings}


def stats_section(date_from=None, date_to=None, rows=None):
    """DB 분석에 넣을 보도자료별 건수 (폴더를 못 읽어도 연결표에 있는 보도자료로)"""
    data = overview(rows=rows, date_from=date_from, date_to=date_to)
    return {
        "summary": data["summary"],
        "by_release": [{"key": r["id"], "date": r["date"], "kind": r["kind"], "title": r["title"],
                        "has_file": r["has_file"]} | r["counts"] for r in data["releases"]],
        "source": data["source"],
        "folder_error": data["folder_error"],
    }
