"""
GoodMorningKOITA 웹 서버

실행: python app.py  →  브라우저에서 http://127.0.0.1:8000
"""

import threading
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

import archive  # noqa: E402
import auth  # noqa: E402
import collector  # noqa: E402  (.env를 먼저 읽은 뒤 import)
import db  # noqa: E402
import exporter  # noqa: E402

# 첫 화면을 여러 번 새로고침해도 API를 매번 부르지 않도록 하는 최소 간격
MIN_COLLECT_INTERVAL = timedelta(minutes=10)

VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()

app = FastAPI(title="GoodMorningKOITA", version=VERSION)
collect_lock = threading.Lock()
archive_lock = threading.Lock()
db.init_db()
archive.tidy()
app.middleware("http")(auth.require_login)


@app.get("/login")
def login_form():
    return auth.login_page()


@app.post("/login")
async def login_submit(request: Request):
    return await auth.login(request)


@app.get("/")
def index():
    # 파일을 바꿨을 때 브라우저가 예전 화면을 캐시해서 보여주지 않도록
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


# ---------------- 키워드 ----------------

class KeywordIn(BaseModel):
    keyword: str


class KeywordPatch(BaseModel):
    keyword: str | None = None
    enabled: bool | None = None


@app.get("/api/keywords")
def get_keywords():
    return db.list_keywords()


@app.post("/api/keywords")
def post_keyword(body: KeywordIn):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(400, "키워드를 입력하세요.")
    try:
        db.add_keyword(keyword)
    except Exception:
        raise HTTPException(409, "이미 등록된 키워드입니다.")
    return db.list_keywords()


@app.patch("/api/keywords/{keyword_id}")
def patch_keyword(keyword_id: int, body: KeywordPatch):
    keyword = body.keyword.strip() if body.keyword is not None else None
    if keyword == "":
        raise HTTPException(400, "키워드를 입력하세요.")
    try:
        db.update_keyword(keyword_id, keyword=keyword, enabled=body.enabled)
    except Exception:
        raise HTTPException(409, "이미 등록된 키워드입니다.")
    return db.list_keywords()


@app.delete("/api/keywords/{keyword_id}")
def remove_keyword(keyword_id: int):
    db.delete_keyword(keyword_id)
    return db.list_keywords()


# ---------------- 수집 ----------------

@app.post("/api/collect")
def run_collect(
    hours: int = Query(24, ge=1, le=24 * 31),
    date_from: str | None = None,
    date_to: str | None = None,
    force: bool = False,
):
    last = db.last_run()
    if not force and last:
        finished = datetime.fromisoformat(last["finished_at"])
        if datetime.now().astimezone() - finished < MIN_COLLECT_INTERVAL:
            return {"skipped": True, "last_run": last}

    since, until = period_datetimes(hours, date_from, date_to)

    if not collect_lock.acquire(blocking=False):
        return {"skipped": True, "busy": True, "last_run": last}
    try:
        keywords = db.enabled_keywords()
        run_id = db.start_run()
        new_articles, keyword_hits, errors = collector.collect(
            keywords, since, until, known_keys=db.known_article_keys()
        )
        new_count = db.save_articles(new_articles, keyword_hits)
        db.finish_run(run_id, new_count, errors)
        return {"skipped": False, "new_count": new_count, "errors": errors, "last_run": db.last_run()}
    finally:
        collect_lock.release()


@app.get("/api/status")
def status():
    return {"last_run": db.last_run(), "version": VERSION, "archive": archive.info()}


# ---------------- 기사 ----------------

def period_datetimes(hours, date_from, date_to):
    """
    hours면 지금부터 hours 시간 전까지,
    날짜를 주면 date_from 00:00 ~ date_to 다음 날 00:00 (한국 시간, 끝 날짜 포함)
    """
    now = datetime.now(collector.KST)
    if not (date_from or date_to):
        return now - timedelta(hours=hours), None
    try:
        start = datetime.fromisoformat(date_from).replace(tzinfo=collector.KST) if date_from else None
        end = datetime.fromisoformat(date_to).replace(tzinfo=collector.KST) + timedelta(days=1) if date_to else None
    except ValueError:
        raise HTTPException(400, "날짜 형식이 올바르지 않습니다.")
    if start and end and start >= end:
        raise HTTPException(400, "시작 날짜가 끝 날짜보다 늦습니다.")
    # 시작 날짜만 없으면 끝 날짜 하루치
    return start or end - timedelta(days=1), end


def period(hours, date_from, date_to):
    since, until = period_datetimes(hours, date_from, date_to)
    as_text = lambda d: d.isoformat(timespec="seconds") if d else None
    return as_text(since), as_text(until)


@app.get("/api/articles")
def get_articles(hours: int = 24, date_from: str | None = None, date_to: str | None = None):
    since, until = period(hours, date_from, date_to)
    rows = db.list_articles(since, until)
    saved = archive.saved_urls()
    for row in rows:
        row["archived"] = row["url"] in saved
    return rows


class ArticlePatch(BaseModel):
    press: str | None = None
    print_page: str | None = None
    has_video: bool | None = None


@app.patch("/api/articles/{article_id}")
def patch_article(article_id: int, body: ArticlePatch):
    db.update_article(article_id, body.press, body.print_page, body.has_video)
    return {"ok": True}


@app.delete("/api/articles/{article_id}")
def remove_article(article_id: int):
    db.delete_article(article_id)
    return {"ok": True}


class Selection(BaseModel):
    ids: list[int]


def selected_articles(body: Selection):
    articles = db.articles_by_ids(body.ids)
    if not articles:
        raise HTTPException(400, "체크한 기사가 없습니다.")
    return articles


@app.post("/api/archive")
def save_to_archive(body: Selection):
    """체크한 기사를 별도 DB(database 폴더)에 이어 붙인다. 이미 있는 행은 건너뛴다."""
    articles = selected_articles(body)
    with archive_lock:
        return archive.save(articles)


@app.post("/api/export")
def export_selected(body: Selection):
    """체크한 기사만 엑셀로"""
    return xlsx_response(selected_articles(body))


@app.get("/api/export")
def export(hours: int = 24, date_from: str | None = None, date_to: str | None = None):
    """기간 전체를 엑셀로 (v1.0.0 방식, 주소로 바로 받을 때)"""
    since, until = period(hours, date_from, date_to)
    return xlsx_response(db.list_articles(since, until))


def xlsx_response(articles):
    content = exporter.build_workbook(articles)
    filename = f"산기협_뉴스_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


if __name__ == "__main__":
    import uvicorn

    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
