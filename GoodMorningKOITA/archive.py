"""
기사 DB: 화면에서 체크한 기사를 엑셀과 같은 행 형태로 계속 쌓아 두는 별도 저장소

다른 프로그램이 읽어 가도록 database/ 폴더에 같은 내용을 세 가지 형식으로 둔다.
- koita_news.sqlite : 원본. 중복 검사와 추가는 여기서 한다
- koita_news.csv    : 저장할 때마다 원본에서 다시 만드는 사본 (엑셀·메모장 등 어디서나 열림)
- koita_news.json   : 저장할 때마다 원본에서 다시 만드는 사본 (웹·스크립트용)

수집용 news.db와 따로 두는 이유: news.db는 검색 결과를 임시로 모아 두는 곳이고,
이 DB는 사람이 골라서 확정한 기사만 남는 곳이라 다른 프로그램이 믿고 쓸 수 있어야 한다.
"""

import csv
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import exporter

# .env의 ARCHIVE_DIR로 다른 폴더(예: 공유 폴더)를 지정할 수 있다
ARCHIVE_DIR = Path(os.getenv("ARCHIVE_DIR", "").strip() or Path(__file__).with_name("database"))
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
]

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
    row_key       TEXT NOT NULL UNIQUE
);
"""


@contextmanager
def connect():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save(articles):
    """
    기사들을 엑셀 행으로 바꿔 DB 맨 아래에 붙인다. 이미 있는 행은 건너뛴다.
    반환: {"added", "skipped", "total", "warnings"}
    """
    rows = exporter.to_rows(articles)
    # 아래로 쌓이므로 한 번에 저장하는 묶음 안에서도 오래된 것부터
    rows.sort(key=lambda r: (r["날짜"], exporter.type_order(r), r["게재일시"]))
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")

    added = 0
    with connect() as conn:
        for r in rows:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO news
                   (date, press, type, title, url, published_at, keywords, saved_at, row_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["날짜"].rstrip(".").replace(".", "-"), r["언론사명"], r["구분"], r["제목"], r["URL"],
                    r["게재일시"], r["키워드"].replace('"', ""), saved_at, exporter.row_key(r),
                ),
            )
            added += cursor.rowcount
        total = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]

    return {"added": added, "skipped": len(rows) - added, "total": total, "warnings": write_copies()}


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
        try:
            fill(temp)
            os.replace(temp, path)
        except PermissionError:
            temp.unlink(missing_ok=True)
            warnings.append(f"{path.name} 파일이 다른 프로그램(엑셀 등)에 열려 있어 갱신하지 못했습니다. "
                            "닫고 다시 DB 저장을 누르면 갱신됩니다. (기사는 SQLite에 저장됐습니다)")

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


def saved_urls():
    """화면에서 '저장됨' 표시를 하려고, DB에 들어간 온라인 기사 URL"""
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT url FROM news WHERE type = '온라인'")}
