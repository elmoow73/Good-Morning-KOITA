"""SQLite 저장소: 키워드, 기사, 수집 기록"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).with_name("news.db")

DEFAULT_KEYWORDS = ["한국산업기술진흥협회", "산기협"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL UNIQUE,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    article_key    TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    description    TEXT,
    press          TEXT,
    print_page     TEXT NOT NULL DEFAULT '온라인',
    has_video      INTEGER NOT NULL DEFAULT 0,
    naver_link     TEXT,
    original_link  TEXT,
    published_at   TEXT NOT NULL,
    collected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);

CREATE TABLE IF NOT EXISTS article_keywords (
    article_id  INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    keyword     TEXT NOT NULL,
    PRIMARY KEY (article_id, keyword)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    new_count    INTEGER DEFAULT 0,
    errors       TEXT
);
"""

# 예전 버전에서 만든 news.db에 나중에 생긴 열을 덧붙인다
MIGRATIONS = {
    ("articles", "notion_page_id"): "ALTER TABLE articles ADD COLUMN notion_page_id TEXT",
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for (table, column), sql in MIGRATIONS.items():
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(sql)
        if conn.execute("SELECT COUNT(*) FROM keywords").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO keywords (keyword, created_at) VALUES (?, ?)",
                [(k, now_iso()) for k in DEFAULT_KEYWORDS],
            )


# ---------------- 키워드 ----------------

def list_keywords():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM keywords ORDER BY id")]


def enabled_keywords():
    with connect() as conn:
        return [r["keyword"] for r in conn.execute("SELECT keyword FROM keywords WHERE enabled = 1 ORDER BY id")]


def add_keyword(keyword):
    with connect() as conn:
        conn.execute("INSERT INTO keywords (keyword, created_at) VALUES (?, ?)", (keyword, now_iso()))


def update_keyword(keyword_id, keyword=None, enabled=None):
    with connect() as conn:
        if keyword is not None:
            conn.execute("UPDATE keywords SET keyword = ? WHERE id = ?", (keyword, keyword_id))
        if enabled is not None:
            conn.execute("UPDATE keywords SET enabled = ? WHERE id = ?", (1 if enabled else 0, keyword_id))


def delete_keyword(keyword_id):
    with connect() as conn:
        conn.execute("DELETE FROM keywords WHERE id = ?", (keyword_id,))


# ---------------- 기사 ----------------

def known_article_keys():
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT article_key FROM articles")}


def save_articles(new_articles, keyword_hits):
    """새 기사를 저장하고, 기사-키워드 연결을 기록한다. 저장된 새 기사 수를 돌려준다."""
    collected_at = now_iso()
    with connect() as conn:
        for a in new_articles:
            conn.execute(
                """INSERT OR IGNORE INTO articles
                   (article_key, title, description, press, print_page, has_video,
                    naver_link, original_link, published_at, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    a.get("naver_link") or a["original_link"], a["title"], a.get("description"),
                    a.get("press", ""), a.get("print_page", "온라인"), 1 if a.get("has_video") else 0,
                    a.get("naver_link"), a["original_link"], a["published_at"], collected_at,
                ),
            )
        for key, keyword in keyword_hits:
            conn.execute(
                """INSERT OR IGNORE INTO article_keywords (article_id, keyword)
                   SELECT id, ? FROM articles WHERE article_key = ?""",
                (keyword, key),
            )
    return len(new_articles)


def list_articles(since_iso=None, until_iso=None, article_id=None, notion_missing=False):
    query = """
        SELECT a.*, GROUP_CONCAT(k.keyword, ', ') AS keywords
        FROM articles a LEFT JOIN article_keywords k ON k.article_id = a.id
        WHERE 1 = 1
    """
    params = []
    if article_id is not None:
        query += " AND a.id = ?"
        params.append(article_id)
    if notion_missing:
        query += " AND a.notion_page_id IS NULL"
    if since_iso:
        query += " AND a.published_at >= ?"
        params.append(since_iso)
    if until_iso:
        query += " AND a.published_at < ?"
        params.append(until_iso)
    query += " GROUP BY a.id ORDER BY a.published_at DESC"
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(query, params)]
    for row in rows:
        row["has_video"] = bool(row["has_video"])
        row["url"] = row["original_link"] or row["naver_link"]
    return rows


def update_article(article_id, press=None, print_page=None, has_video=None):
    """잘못 판별된 언론사명·지면·영상을 화면에서 고칠 때 사용"""
    with connect() as conn:
        if press is not None:
            conn.execute("UPDATE articles SET press = ? WHERE id = ?", (press, article_id))
        if print_page is not None:
            conn.execute("UPDATE articles SET print_page = ? WHERE id = ?", (print_page or "온라인", article_id))
        if has_video is not None:
            conn.execute("UPDATE articles SET has_video = ? WHERE id = ?", (1 if has_video else 0, article_id))


def get_article(article_id):
    rows = list_articles(article_id=article_id)
    return rows[0] if rows else None


def set_notion_page_ids(pairs):
    """[(기사 id, 노션 페이지 id들)]를 기록해 같은 기사를 노션에 두 번 보내지 않게 한다. None이면 다시 보낼 대상이 된다."""
    with connect() as conn:
        conn.executemany("UPDATE articles SET notion_page_id = ? WHERE id = ?", [(p, a) for a, p in pairs])


def delete_article(article_id):
    with connect() as conn:
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))


# ---------------- 수집 기록 ----------------

def start_run():
    with connect() as conn:
        return conn.execute("INSERT INTO runs (started_at) VALUES (?)", (now_iso(),)).lastrowid


def finish_run(run_id, new_count, errors):
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, new_count = ?, errors = ? WHERE id = ?",
            (now_iso(), new_count, "\n".join(errors) or None, run_id),
        )


def last_run():
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
