"""
기사 DB: 화면에서 체크한 기사를 엑셀과 같은 행 형태로 계속 쌓아 두는 별도 저장소

다른 프로그램이 읽어 가도록 database/ 폴더에 같은 내용을 세 가지 형식으로 둔다. v1.4.0부터 연도별 파일
(기사 '날짜' 열의 연도. 2027년 기사가 처음 저장되면 koita_news_2027.*이 새로 생긴다):
- koita_news_2026.sqlite : 원본. 중복 검사와 추가는 여기서 하고, 저장할 때마다 게재일시 최신순으로 다시 정렬한다
- koita_news_2026.csv    : 저장할 때마다 원본에서 다시 만드는 사본 (엑셀·메모장 등 어디서나 열림)
- koita_news_2026.json   : 저장할 때마다 원본에서 다시 만드는 사본 (웹·스크립트용)
연번은 연도마다 1부터. 예전 하나짜리 koita_news.*는 처음 켤 때 연도별로 나눠 옮기고 database/_이전에 백업한다.
.env ARCHIVE_COPY_DIR가 있으면 DB가 바뀔 때마다 세 파일을 그 폴더(예: 회사 드롭박스)에도 덮어쓴다 (보기용 복사본).

수집용 news.db와 따로 두는 이유: news.db는 검색 결과를 임시로 모아 두는 곳이고,
이 DB는 사람이 골라서 확정한 기사만 남는 곳이라 다른 프로그램이 믿고 쓸 수 있어야 한다.
"""

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import exporter

# .env의 ARCHIVE_DIR로 다른 폴더(예: 공유 폴더)를 지정할 수 있다.
# 없으면 DATA_DIR/database, DATA_DIR도 없으면 프로그램 폴더의 database
ARCHIVE_DIR = Path(
    os.getenv("ARCHIVE_DIR", "").strip()
    or Path(os.getenv("DATA_DIR", "").strip() or Path(__file__).parent) / "database"
)
DB_PREFIX = "koita_news"
LEGACY_NAME = "koita_news"  # v1.3.0까지 쓰던 하나짜리 DB (연도 없음)
BACKUP_DIR_NAME = "_이전"
# DB가 바뀔 때마다 세 파일을 덮어쓸 복사본 폴더 (비우면 안 함). 여러 곳이면 ; 로 나눈다
COPY_DIRS = [Path(p.strip()) for p in os.getenv("ARCHIVE_COPY_DIR", "").split(";") if p.strip()]


def this_year():
    return datetime.now().year


def year_of(row):
    """행이 들어갈 연도 DB: '날짜' 열의 연도 (없거나 이상하면 올해)"""
    m = re.match(r"(\d{4})", str(row.get("date") or ""))
    return int(m.group(1)) if m else this_year()


def paths(year):
    base = ARCHIVE_DIR / f"{DB_PREFIX}_{year}"
    return {"sqlite": base.with_suffix(".sqlite"), "csv": base.with_suffix(".csv"), "json": base.with_suffix(".json")}


def years():
    """연도별 DB가 있는 연도들 (오래된 순). 하나도 없으면 [올해]"""
    found = sorted({int(m.group(1)) for p in ARCHIVE_DIR.glob(f"{DB_PREFIX}_*.sqlite")
                    if (m := re.fullmatch(rf"{DB_PREFIX}_(\d{{4}})\.sqlite", p.name))})
    return found or [this_year()]

# (DB 열 이름, CSV 머리글)
COLUMNS = [
    ("seq", "연번"),
    ("date", "날짜"),
    ("press", "언론사명"),
    ("type", "구분"),
    ("title", "제목"),
    ("url", "URL"),
    ("published_at", "게재일시"),
    ("keywords", "키워드"),
    ("saved_at", "저장일시"),
    ("category", "분류"),  # v1.3.0: 맨 오른쪽. 고르지 않았으면 빈 값
]

# 분류 값 (화면의 드롭다운 순서). 빈 값 = 아직 고르지 않음("선택")
CATEGORIES = ["보도자료", "특별보도", "인용", "장영실상", "엔지니어상", "협력", "언급", "이슈", "정부사업", "기타"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    press         TEXT NOT NULL,
    type          TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    keywords      TEXT NOT NULL,
    saved_at      TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT '',
    row_key       TEXT NOT NULL UNIQUE
);
"""

# v1.2.0 이전 DB에 분류 열을 더할 때: 표를 새로 만들어 저장일시 바로 오른쪽(맨 오른쪽 열)에 둔다
MIGRATE_CATEGORY = """
CREATE TABLE news_v13 (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    press         TEXT NOT NULL,
    type          TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    keywords      TEXT NOT NULL,
    saved_at      TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT '',
    row_key       TEXT NOT NULL UNIQUE
);
INSERT INTO news_v13 (seq, date, press, type, title, url, published_at, keywords, saved_at, row_key)
    SELECT seq, date, press, type, title, url, published_at, keywords, saved_at, row_key FROM news;
DROP TABLE news;
ALTER TABLE news_v13 RENAME TO news;
"""


@contextmanager
def connect(year=None, path=None):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path or paths(year or this_year())["sqlite"])
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        if "category" not in [r[1] for r in conn.execute("PRAGMA table_info(news)")]:
            conn.executescript(MIGRATE_CATEGORY)
        yield conn
        conn.commit()
    finally:
        conn.close()


def duplicate_keys(row):
    """중복 기준은 엑셀과 같다: 온라인·방송은 (URL, 구분), 지면은 (날짜, 언론사명, 구분, 제목)"""
    return {row["row_key"]}


def sort_key(row):
    """게재일시(published_at) 최신순. 같은 기사의 행은 지면 → 방송 → 온라인"""
    return (row["published_at"], -exporter.type_order({"구분": row["type"]}), row["title"])


def rewrite(conn, rows):
    """
    중복을 지우고 기사 날짜순으로 정렬해서 표를 다시 쓴다. 연번(seq)은 정렬된 순서대로 1부터 다시 매긴다.
    rows는 먼저 저장된 것부터 들어 있어야 한다 (중복이면 먼저 저장된 행을 남긴다).
    반환: 남은 행 목록
    """
    seen = set()
    kept = []
    for row in rows:
        keys = duplicate_keys(row)
        if keys & seen:
            continue
        seen |= keys
        kept.append(row)
    kept.sort(key=sort_key, reverse=True)

    names = [name for name, _ in COLUMNS if name != "seq"] + ["row_key"]
    conn.execute("DELETE FROM news")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'news'")
    conn.executemany(
        f"INSERT INTO news (seq, {', '.join(names)}) VALUES ({', '.join('?' * (len(names) + 1))})",
        [(seq, *(row[n] for n in names)) for seq, row in enumerate(kept, start=1)],
    )
    return kept


def stored_rows(conn):
    # 저장일시, 예전 연번 순 = 먼저 저장된 순
    return [dict(r) for r in conn.execute("SELECT * FROM news ORDER BY saved_at, seq")]


def is_print(row_type):
    return row_type not in ("온라인", "방송")


def make_row_key(row):
    """DB 행(date가 2026-10-27 형식)의 중복 키. 엑셀 행 규칙(exporter.row_key)과 같은 값"""
    return exporter.row_key({
        "날짜": row["date"].replace("-", ".") + ".", "언론사명": row["press"], "구분": row["type"],
        "제목": row["title"], "URL": row["url"],
    })


def save(articles):
    """
    기사들을 엑셀 행으로 바꿔 연도별 DB에 더한다. 이미 있는 기사는 건너뛰고, 전체를 기사 날짜순으로 다시 정렬한다.
    이미 있는 행이라도 이번에 분류를 골라 저장했으면 그 행의 분류를 새 값으로 덮어쓴다.
    반환: {"added", "skipped", "categorized", "removed", "total", "years", "warnings"}
    """
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    new_rows = [
        {
            "date": r["날짜"].rstrip(".").replace(".", "-"), "press": r["언론사명"], "type": r["구분"],
            "title": r["제목"], "url": r["URL"], "published_at": r["게재일시"],
            "keywords": r["키워드"].replace('"', ""), "saved_at": saved_at,
            "category": r.get("분류") or "", "row_key": exporter.row_key(r),
        }
        for r in exporter.to_rows(articles)
    ]

    # 지면 행은 URL이 "-"라 날짜를 편집기에서 고치면 키가 달라진다. 같은 언론사·제목의 지면 행이 이미 있으면
    # (어느 연도 DB든) 그 행과 같은 행으로 보고 새로 넣지 않는다 (날짜·지면 번호가 달라도. 사용자 결정 2026-11-03)
    existing_by_year = {}
    print_keys = {}
    for year in years():
        with connect(year) as conn:
            existing_by_year[year] = stored_rows(conn)
        for r in existing_by_year[year]:
            if is_print(r["type"]):
                print_keys[(r["press"], r["title"])] = (year, r["row_key"])
    # 지면 확인에서 지면 행 제목을 지면 기사 제목으로 바꾼 경우, 예전(온라인) 제목으로 와도 같은 행으로 본다
    for _ in range(3):  # 여러 번 바꾼 경우까지
        for a in load_title_aliases():
            if (a["press"], a["new"]) in print_keys:
                print_keys.setdefault((a["press"], a["old"]), print_keys[(a["press"], a["new"])])
    by_year = {}
    for row in new_rows:
        year = year_of(row)
        if is_print(row["type"]) and (row["press"], row["title"]) in print_keys:
            year, row["row_key"] = print_keys[(row["press"], row["title"])]
        by_year.setdefault(year, []).append(row)

    added = categorized = removed = 0
    warnings = []
    for year, rows in sorted(by_year.items()):
        with connect(year) as conn:
            existing = stored_rows(conn)
            # 이미 저장된 행의 분류 덮어쓰기 (고르지 않은 "선택"은 예전 값을 그대로 둔다)
            chosen = {r["row_key"]: r["category"] for r in rows if r["category"]}
            for row in existing:
                if row["row_key"] in chosen and row["category"] != chosen[row["row_key"]]:
                    row["category"] = chosen[row["row_key"]]
                    categorized += 1
            before = len(rewrite(conn, existing))  # 예전부터 있던 중복도 이번에 정리된다
            total = len(rewrite(conn, existing + rows))
        added += total - before
        removed += len(existing) - before
        warnings += write_copies(year)
    return {
        "added": added, "skipped": len(new_rows) - added, "categorized": categorized,
        "removed": removed, "total": info()["total"], "years": sorted(by_year), "warnings": warnings,
    }


def migrate_legacy():
    """
    v1.3.0까지의 하나짜리 koita_news.sqlite를 연도별 DB(koita_news_2026 …)로 나눠 옮긴다 (처음 켤 때 한 번).
    이미 연도별 DB가 있으면 합친다(같은 행은 하나만). 예전 파일(sqlite·csv·json)은 database/_이전으로 옮겨 둔다.
    반환: {연도: 옮긴 행 수}
    """
    legacy = ARCHIVE_DIR / f"{LEGACY_NAME}.sqlite"
    if not legacy.exists():
        return {}
    with connect(path=legacy) as conn:
        rows = stored_rows(conn)
    moved = {}
    for row in rows:
        moved.setdefault(year_of(row), []).append(row)
    for year, part in moved.items():
        with connect(year) as conn:
            rewrite(conn, stored_rows(conn) + part)
    backup = ARCHIVE_DIR / BACKUP_DIR_NAME
    backup.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for old in ARCHIVE_DIR.glob(f"{LEGACY_NAME}.*"):
        shutil.move(str(old), str(backup / f"{old.stem}_{stamp}{old.suffix}"))
    for old in ARCHIVE_DIR.glob(f"{LEGACY_NAME}_갱신대기.*"):
        shutil.move(str(old), str(backup / f"{old.stem}_{stamp}{old.suffix}"))
    for year in moved:
        write_copies(year)
    return {year: len(part) for year, part in moved.items()}


def tidy():
    """프로그램을 켤 때 예전 DB를 연도별로 옮기고, 중복을 정리해 날짜순으로 맞춘다. 정리한 행 수를 돌려준다."""
    migrate_legacy()
    removed = 0
    for year in years():
        path = paths(year)["sqlite"]
        if not path.exists():
            continue
        with connect(year) as conn:
            existing = stored_rows(conn)
            if not existing:
                continue
            removed += len(existing) - len(rewrite(conn, existing))
        write_copies(year)
    return removed


def all_rows(year=None):
    """year 연도 DB의 행 (연번순). year가 None이면 모든 연도 DB의 행 (최근 연도부터)"""
    names = ", ".join(name for name, _ in COLUMNS)
    if year is None:
        rows = []
        for y in sorted(years(), reverse=True):
            if paths(y)["sqlite"].exists():
                rows += all_rows(y)
        return rows
    with connect(year) as conn:
        return [dict(r) for r in conn.execute(f"SELECT {names} FROM news ORDER BY seq")]


def write_file(path, fill):
    """
    fill(임시 파일)로 다 쓴 뒤 path로 바꿔치기한다. 엑셀 등에 열려 있어 못 바꾸면 옆에 _갱신대기 파일로 두고
    안내 문구를 돌려준다 (문제없으면 None). 기사 DB·보도자료 연결표의 CSV·JSON 사본이 함께 쓴다.
    """
    temp = path.with_suffix(path.suffix + ".tmp")
    waiting = path.with_name(f"{path.stem}_갱신대기{path.suffix}")
    fill(temp)
    # 엑셀로 열려 있거나 OneDrive가 동기화하느라 잠깐 잠긴 경우가 있어 몇 번 다시 시도한다
    for attempt in range(5):
        try:
            os.replace(temp, path)
            waiting.unlink(missing_ok=True)  # 원래 파일에 저장됐으니 대기 파일은 필요 없다
            return None
        except PermissionError:
            time.sleep(1)
    # 그래도 잠겨 있으면 옆에 최신 내용을 따로 저장해 둔다. 다음 저장 때 원래 파일로 다시 쓴다
    try:
        os.replace(temp, waiting)
        place = f"최신 내용은 {waiting.name}에 저장해 두었고"
    except PermissionError:
        temp.unlink(missing_ok=True)
        place = "내용은 SQLite에 저장됐고"
    return (f"{path.name} 파일이 다른 프로그램(엑셀 등)에 열려 있어 갱신하지 못했습니다. "
            f"{place}, 파일을 닫고 다시 저장하면 {path.name}에 반영됩니다.")


def write_table_copies(files, columns, rows):
    """files(sqlite·csv·json 경로)의 CSV·JSON 사본을 rows로 다시 만든다. columns: [(열 이름, CSV 머리글)]"""
    def fill_csv(temp):
        # utf-8-sig: 엑셀에서 열어도 한글이 깨지지 않도록
        with open(temp, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([header for _, header in columns])
            writer.writerows([r[name] for name, _ in columns] for r in rows)

    def fill_json(temp):
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)

    warnings = [write_file(files["csv"], fill_csv), write_file(files["json"], fill_json)]
    return [w for w in warnings if w] + copy_files(files)


def write_copies(year):
    """
    year 연도 DB의 CSV·JSON 사본을 원본과 똑같이 다시 만들고, ARCHIVE_COPY_DIR(드롭박스 등)에도 세 파일을 덮어쓴다.
    파일이 열려 있거나 복사본 폴더를 못 쓰면 안내 문구를 돌려준다 (원본 저장은 그대로 됨).
    """
    return write_table_copies(paths(year), COLUMNS, all_rows(year))


def copy_out(year):
    """ARCHIVE_COPY_DIR 폴더에 year 연도 DB 세 파일을 덮어쓴다 (보기용 복사본)"""
    return copy_files(paths(year))


def copy_files(files):
    """
    ARCHIVE_COPY_DIR 폴더에 files(sqlite·csv·json) 세 파일을 덮어쓴다 (보기용 복사본).
    드롭박스가 반쯤 쓴 파일을 올리지 않도록 같은 폴더의 임시 파일에 다 쓴 뒤 한 번에 바꾼다. 실패하면 안내 문구
    """
    warnings = []
    for folder in COPY_DIRS:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for kind, src in files.items():
                if not src.exists():
                    continue
                dest = folder / src.name
                temp = folder / f".{src.name}.tmp"
                if kind == "sqlite":
                    # SQLite는 쓰는 중에도 온전한 사본이 되도록 backup API로 복사
                    temp.unlink(missing_ok=True)
                    source, target = sqlite3.connect(src), sqlite3.connect(temp)
                    try:
                        source.backup(target)
                    finally:
                        source.close()
                        target.close()
                else:
                    shutil.copyfile(src, temp)
                for attempt in range(5):
                    try:
                        os.replace(temp, dest)
                        break
                    except PermissionError:
                        time.sleep(1)
                else:
                    temp.unlink(missing_ok=True)
                    raise PermissionError(f"{dest.name}이(가) 열려 있습니다")
        except OSError as exc:
            warnings.append(f"드롭박스(복사본) 복사 실패: {folder} ({exc}). 원본은 저장됐고, 다음에 DB가 바뀔 때 다시 복사합니다.")
    return warnings


def info():
    """기사 DB 요약: 전체 행 수, 연도별 행 수, 폴더, 복사본 폴더"""
    by_year = {}
    for year in years():
        if paths(year)["sqlite"].exists():
            with connect(year) as conn:
                by_year[year] = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    return {"total": sum(by_year.values()), "by_year": by_year, "year": this_year(),
            "year_total": by_year.get(this_year(), 0), "folder": str(ARCHIVE_DIR.resolve()),
            "copy_dirs": [str(p) for p in COPY_DIRS]}


def aliases_path():
    return ARCHIVE_DIR / "title_aliases.json"


def load_title_aliases():
    """[{"press", "old", "new"}]: 지면 행 제목을 바꾼 기록 (예전 온라인 제목 → 지면 기사 제목)"""
    try:
        return json.loads(aliases_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def add_title_alias(press, old, new):
    """
    지면 행 제목을 바꾼 기록을 남긴다. 기사 목록에서 같은 기사를 다시 DB 저장할 때
    예전 제목의 지면 행이 또 생기지 않게 하려고 쓴다.
    """
    aliases = [a for a in load_title_aliases() if not (a["press"] == press and a["old"] == old)]
    aliases.append({"press": press, "old": old, "new": new})
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    temp = aliases_path().with_suffix(".tmp")
    temp.write_text(json.dumps(aliases, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, aliases_path())


def saved_categories():
    """화면에서 '저장됨' 표시와 분류를 보여 주려고, DB(모든 연도)에 들어간 온라인 기사 {URL: 분류}"""
    out = {}
    for year in years():
        if paths(year)["sqlite"].exists():
            with connect(year) as conn:
                out.update({r[0]: r[1] for r in conn.execute("SELECT url, category FROM news WHERE type = '온라인'")})
    return out


# ---------------- DB 편집기 ----------------

def revision(rows):
    """DB 내용의 지문. 편집기를 연 뒤 다른 곳(DB 저장 등)에서 DB가 바뀌었는지 알아보는 데 쓴다"""
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def editor_data(year=None):
    """편집기에 줄 year 연도 DB (비우면 올해)"""
    year = int(year or this_year())
    rows = all_rows(year)
    return {"rows": rows, "revision": revision(rows), "categories": CATEGORIES, "year": year,
            "years": sorted(set(years()) | {this_year(), year}),
            "file": paths(year)["sqlite"].name,
            "columns": [{"name": n, "header": h} for n, h in COLUMNS]}


class EditError(Exception):
    """편집한 내용에 문제가 있을 때. problems: [{"index", "seq", "field", "message"}]"""

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or []


def clean_edited_rows(rows, old_by_seq, saved_at):
    """편집기에서 온 행을 검사하고 DB 행 형태로 다듬는다. 문제가 있으면 EditError"""
    problems = []
    cleaned = []

    def problem(index, row, field, message):
        problems.append({"index": index, "seq": row.get("seq"), "field": field, "message": message})

    for index, raw in enumerate(rows):
        row = {name: str(raw.get(name) if raw.get(name) is not None else "").strip() for name, _ in COLUMNS}
        row["seq"] = raw.get("seq")
        row["date"] = row["date"].replace(".", "-").rstrip("-")
        try:
            row["date"] = datetime.strptime(row["date"], "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            problem(index, row, "date", "날짜는 2026-10-27 형식으로 넣으세요.")
        for field, label in (("press", "언론사명"), ("type", "구분"), ("title", "제목")):
            if not row[field]:
                problem(index, row, field, f"{label}을(를) 넣으세요.")
        if is_print(row["type"]):
            row["url"] = row["url"] or "-"
        elif not row["url"].startswith(("http://", "https://")):
            problem(index, row, "url", "온라인·방송 행은 http로 시작하는 URL이 필요합니다.")
        if not row["published_at"] and "date" not in {p["field"] for p in problems if p["index"] == index}:
            row["published_at"] = f"{row['date']}T00:00:00+09:00"
        try:
            datetime.fromisoformat(row["published_at"])
        except ValueError:
            problem(index, row, "published_at", "게재일시는 2026-10-27T09:00:00+09:00 형식으로 넣으세요.")
        if row["category"] and row["category"] not in CATEGORIES:
            problem(index, row, "category", f"분류는 {', '.join(CATEGORIES)} 중 하나여야 합니다.")
        old = old_by_seq.get(row["seq"])
        row["saved_at"] = old["saved_at"] if old else saved_at  # 저장일시는 고칠 수 없다
        cleaned.append(row)

    seen = {}
    for index, row in enumerate(cleaned):
        if any(p["index"] == index for p in problems):
            continue
        row["row_key"] = make_row_key(row)
        if row["row_key"] in seen:
            other = seen[row["row_key"]]
            where = f"연번 {cleaned[other]['seq']}" if cleaned[other]["seq"] else f"{other + 1}번째 행"
            field = "type" if is_print(row["type"]) else "url"
            problem(index, row, field, f"{where}과(와) 같은 행입니다(중복). 한쪽을 지우거나 고치세요.")
        else:
            seen[row["row_key"]] = index

    if problems:
        raise EditError(f"고칠 곳이 {len(problems)}군데 있습니다.", problems)
    return cleaned


def replace_all(rows, base_revision, year=None):
    """
    편집기에서 고친 year 연도 DB 전체 행으로 그 DB를 바꾼다. SQLite를 바꾼 뒤 CSV·JSON 사본도 똑같이 다시 만든다.
    날짜를 다른 해로 고쳤거나 다른 해 날짜로 넣은 행은 그 해 DB로 옮긴다(같은 행이 있으면 하나만).
    base_revision: 편집기를 열 때 받은 지문. 그사이 DB가 바뀌었으면 덮어쓰지 않고 EditError
    """
    year = int(year or this_year())
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with connect(year) as conn:
        names = ", ".join(name for name, _ in COLUMNS)
        current = [dict(r) for r in conn.execute(f"SELECT {names} FROM news ORDER BY seq")]
        if revision(current) != base_revision:
            raise EditError("편집기를 연 뒤 다른 곳에서 DB가 바뀌었습니다. 다시 불러온 뒤 고쳐 주세요.")
        old_by_seq = {r["seq"]: r for r in current}
        cleaned = clean_edited_rows(rows, old_by_seq, saved_at)
        here = [r for r in cleaned if year_of(r) == year]
        elsewhere = [r for r in cleaned if year_of(r) != year]
        # 예전 행은 먼저 저장된 순서를 지키고, 새로 넣은 행은 뒤에
        here.sort(key=lambda r: (r["saved_at"], r["seq"] or 10**9))
        kept = rewrite(conn, here)
        kept_seqs = {r["seq"] for r in here if r["seq"]}
    warnings = write_copies(year)
    moved = {}
    for row in elsewhere:
        moved.setdefault(year_of(row), []).append(row)
    for other, part in sorted(moved.items()):
        with connect(other) as conn:
            rewrite(conn, stored_rows(conn) + part)
        warnings += write_copies(other)
    return {
        "total": len(kept),
        "added": sum(1 for r in here if not r["seq"]),
        "deleted": len(set(old_by_seq) - kept_seqs - {r["seq"] for r in elsewhere}),
        "moved": {str(k): len(v) for k, v in moved.items()},
        "warnings": warnings,
    }
