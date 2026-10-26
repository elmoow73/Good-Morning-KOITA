"""
기사 DB: 화면에서 체크한 기사를 엑셀과 같은 행 형태로 계속 쌓아 두는 별도 저장소

다른 프로그램이 읽어 가도록 database/ 폴더에 같은 내용을 세 가지 형식으로 둔다.
- koita_news.sqlite : 원본. 중복 검사와 추가는 여기서 하고, 저장할 때마다 기사 날짜 최신순으로 다시 정렬한다
- koita_news.csv    : 저장할 때마다 원본에서 다시 만드는 사본 (엑셀·메모장 등 어디서나 열림)
- koita_news.json   : 저장할 때마다 원본에서 다시 만드는 사본 (웹·스크립트용)

수집용 news.db와 따로 두는 이유: news.db는 검색 결과를 임시로 모아 두는 곳이고,
이 DB는 사람이 골라서 확정한 기사만 남는 곳이라 다른 프로그램이 믿고 쓸 수 있어야 한다.
"""

import csv
import json
import os
import sqlite3
import time
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


def duplicate_keys(row):
    """중복 기준은 엑셀과 같다: 온라인·방송은 (URL, 구분), 지면은 (날짜, 언론사명, 구분, 제목)"""
    return {row["row_key"]}


def sort_key(row):
    """기사 날짜 최신순 (엑셀과 같음). 같은 날짜는 지면 → 방송 → 온라인, 그다음 게재 시각 최신순"""
    return (row["date"], -exporter.type_order({"구분": row["type"]}), row["published_at"], row["title"])


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


def save(articles):
    """
    기사들을 엑셀 행으로 바꿔 DB에 더한다. 이미 있는 기사는 건너뛰고, 전체를 기사 날짜순으로 다시 정렬한다.
    반환: {"added", "skipped", "removed", "total", "warnings"}
    """
    saved_at = datetime.now().astimezone().isoformat(timespec="seconds")
    new_rows = [
        {
            "date": r["날짜"].rstrip(".").replace(".", "-"), "press": r["언론사명"], "type": r["구분"],
            "title": r["제목"], "url": r["URL"], "published_at": r["게재일시"],
            "keywords": r["키워드"].replace('"', ""), "saved_at": saved_at, "row_key": exporter.row_key(r),
        }
        for r in exporter.to_rows(articles)
    ]

    with connect() as conn:
        existing = stored_rows(conn)
        before = len(rewrite(conn, existing))  # 예전부터 있던 중복도 이번에 정리된다
        total = len(rewrite(conn, existing + new_rows))

    added = total - before
    return {
        "added": added, "skipped": len(new_rows) - added, "removed": len(existing) - before,
        "total": total, "warnings": write_copies(),
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


def saved_urls():
    """화면에서 '저장됨' 표시를 하려고, DB에 들어간 온라인 기사 URL"""
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT url FROM news WHERE type = '온라인'")}
