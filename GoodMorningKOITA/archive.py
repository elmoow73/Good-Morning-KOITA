"""
기사 DB: 화면에서 체크한 기사를 엑셀과 같은 행 형태로 계속 쌓아 두는 별도 저장소

다른 프로그램이 읽어 가도록 database/ 폴더에 같은 내용을 세 가지 형식으로 둔다.
- koita_news.sqlite : 원본. 중복 검사와 추가는 여기서 하고, 저장할 때마다 게재일시 최신순으로 다시 정렬한다
- koita_news.csv    : 저장할 때마다 원본에서 다시 만드는 사본 (엑셀·메모장 등 어디서나 열림)
- koita_news.json   : 저장할 때마다 원본에서 다시 만드는 사본 (웹·스크립트용)

수집용 news.db와 따로 두는 이유: news.db는 검색 결과를 임시로 모아 두는 곳이고,
이 DB는 사람이 골라서 확정한 기사만 남는 곳이라 다른 프로그램이 믿고 쓸 수 있어야 한다.
"""

import csv
import hashlib
import json
import os
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
SQLITE_PATH = ARCHIVE_DIR / "koita_news.sqlite"
CSV_PATH = ARCHIVE_DIR / "koita_news.csv"
JSON_PATH = ARCHIVE_DIR / "koita_news.json"

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
def connect():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
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
    기사들을 엑셀 행으로 바꿔 DB에 더한다. 이미 있는 기사는 건너뛰고, 전체를 기사 날짜순으로 다시 정렬한다.
    이미 있는 행이라도 이번에 분류를 골라 저장했으면 그 행의 분류를 새 값으로 덮어쓴다.
    반환: {"added", "skipped", "categorized", "removed", "total", "warnings"}
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

    with connect() as conn:
        existing = stored_rows(conn)
        # 지면 행은 URL이 "-"라 날짜를 편집기에서 고치면 키가 달라진다. 같은 언론사·제목의 지면 행이 이미 있으면
        # 그 행과 같은 행으로 보고 새로 넣지 않는다 (날짜·지면 번호가 달라도. 사용자 결정 2026-11-03)
        print_keys = {(r["press"], r["title"]): r["row_key"] for r in existing if is_print(r["type"])}
        for row in new_rows:
            if is_print(row["type"]):
                row["row_key"] = print_keys.get((row["press"], row["title"]), row["row_key"])
        # 이미 저장된 행의 분류 덮어쓰기 (고르지 않은 "선택"은 예전 값을 그대로 둔다)
        chosen = {r["row_key"]: r["category"] for r in new_rows if r["category"]}
        categorized = 0
        for row in existing:
            if row["row_key"] in chosen and row["category"] != chosen[row["row_key"]]:
                row["category"] = chosen[row["row_key"]]
                categorized += 1
        before = len(rewrite(conn, existing))  # 예전부터 있던 중복도 이번에 정리된다
        total = len(rewrite(conn, existing + new_rows))

    added = total - before
    return {
        "added": added, "skipped": len(new_rows) - added, "categorized": categorized,
        "removed": len(existing) - before, "total": total, "warnings": write_copies(),
    }


def tidy():
    """프로그램을 켤 때 예전 DB의 중복을 정리하고 날짜순으로 맞춘다. 정리한 행 수를 돌려준다."""
    with connect() as conn:
        existing = stored_rows(conn)
        if not existing:
            return 0
        removed = len(existing) - len(rewrite(conn, existing))
    write_copies()
    return removed


def all_rows():
    names = ", ".join(name for name, _ in COLUMNS)
    with connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT {names} FROM news ORDER BY seq")]


def write_copies():
    """CSV·JSON 사본을 원본과 똑같이 다시 만든다. 파일이 열려 있어 못 쓰면 안내 문구를 돌려준다."""
    rows = all_rows()
    warnings = []

    def write(path, fill):
        temp = path.with_suffix(path.suffix + ".tmp")
        waiting = path.with_name(f"{path.stem}_갱신대기{path.suffix}")
        fill(temp)
        # 엑셀로 열려 있거나 OneDrive가 동기화하느라 잠깐 잠긴 경우가 있어 몇 번 다시 시도한다
        for attempt in range(5):
            try:
                os.replace(temp, path)
                waiting.unlink(missing_ok=True)  # 원래 파일에 저장됐으니 대기 파일은 필요 없다
                return
            except PermissionError:
                time.sleep(1)
        # 그래도 잠겨 있으면 옆에 최신 내용을 따로 저장해 둔다. 다음 저장 때 원래 파일로 다시 쓴다
        try:
            os.replace(temp, waiting)
            place = f"최신 내용은 {waiting.name}에 저장해 두었고"
        except PermissionError:
            temp.unlink(missing_ok=True)
            place = "기사는 SQLite에 저장됐고"
        warnings.append(f"{path.name} 파일이 다른 프로그램(엑셀 등)에 열려 있어 갱신하지 못했습니다. "
                        f"{place}, 파일을 닫고 다시 DB 저장을 누르면 {path.name}에 반영됩니다.")

    def fill_csv(temp):
        # utf-8-sig: 엑셀에서 열어도 한글이 깨지지 않도록
        with open(temp, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([header for _, header in COLUMNS])
            writer.writerows([r[name] for name, _ in COLUMNS] for r in rows)

    def fill_json(temp):
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)

    write(CSV_PATH, fill_csv)
    write(JSON_PATH, fill_json)
    return warnings


def info():
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    return {"total": total, "folder": str(ARCHIVE_DIR.resolve())}


def saved_categories():
    """화면에서 '저장됨' 표시와 분류를 보여 주려고, DB에 들어간 온라인 기사 {URL: 분류}"""
    with connect() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT url, category FROM news WHERE type = '온라인'")}


# ---------------- DB 편집기 ----------------

def revision(rows):
    """DB 내용의 지문. 편집기를 연 뒤 다른 곳(DB 저장 등)에서 DB가 바뀌었는지 알아보는 데 쓴다"""
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def editor_data():
    rows = all_rows()
    return {"rows": rows, "revision": revision(rows), "categories": CATEGORIES,
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


def replace_all(rows, base_revision):
    """
    편집기에서 고친 전체 행으로 DB를 바꾼다. SQLite를 바꾼 뒤 CSV·JSON 사본도 똑같이 다시 만든다.
    base_revision: 편집기를 열 때 받은 지문. 그사이 DB가 바뀌었으면 덮어쓰지 않고 EditError
    """
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    with connect() as conn:
        names = ", ".join(name for name, _ in COLUMNS)
        current = [dict(r) for r in conn.execute(f"SELECT {names} FROM news ORDER BY seq")]
        if revision(current) != base_revision:
            raise EditError("편집기를 연 뒤 다른 곳에서 DB가 바뀌었습니다. 다시 불러온 뒤 고쳐 주세요.")
        old_by_seq = {r["seq"]: r for r in current}
        cleaned = clean_edited_rows(rows, old_by_seq, saved_at)
        # 예전 행은 먼저 저장된 순서를 지키고, 새로 넣은 행은 뒤에
        cleaned.sort(key=lambda r: (r["saved_at"], r["seq"] or 10**9))
        kept = rewrite(conn, cleaned)
        kept_seqs = {r["seq"] for r in cleaned if r["seq"]}
    return {
        "total": len(kept),
        "added": sum(1 for r in cleaned if not r["seq"]),
        "deleted": len(set(old_by_seq) - kept_seqs),
        "warnings": write_copies(),
    }
