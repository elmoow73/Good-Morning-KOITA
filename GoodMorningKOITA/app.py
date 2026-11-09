"""
GoodMorningKOITA 웹 서버

실행: python app.py  →  브라우저에서 http://127.0.0.1:8000
"""

import asyncio
import os
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

import archive  # noqa: E402
import auth  # noqa: E402
import collector  # noqa: E402  (.env를 먼저 읽은 뒤 import)
import db  # noqa: E402
import exporter  # noqa: E402
import printcheck  # noqa: E402
import stats  # noqa: E402

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


# ---------------- 앱 설치(PWA)·상태 확인 ----------------

@app.get("/healthz")
def healthz():
    """로그인 없이 부르는 상태 확인 (클라우드 서버·모니터링·AI 에이전트용). 정상이면 200"""
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return JSONResponse({"ok": False, "version": VERSION, "error": str(exc)}, status_code=503)
    return {"ok": True, "version": VERSION}


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # 서비스 워커는 앱 전체(/)를 맡도록 최상위 주소에서 내려준다
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/icons/{name}")
def icon(name: str):
    path = BASE_DIR / "static" / "icons" / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
def index():
    # 파일을 바꿨을 때 브라우저가 예전 화면을 캐시해서 보여주지 않도록
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/analysis")
def analysis():
    """기사 DB 분석 화면 (기간별 구분·년도·월·분류·언론사 건수)"""
    return FileResponse(BASE_DIR / "static" / "analysis.html", headers={"Cache-Control": "no-store"})


@app.get("/api/archive/stats")
def archive_stats(date_from: str | None = None, date_to: str | None = None):
    """기사 DB 분석 결과 JSON (DB 행 수 기준). date_from·date_to: YYYY-MM-DD, 비우면 전체"""
    return stats.summarize(archive.all_rows(), date_from or None, date_to or None)


@app.get("/editor")
def editor():
    """기사 DB 편집기 (엑셀처럼 직접 고치는 화면)"""
    return FileResponse(BASE_DIR / "static" / "editor.html", headers={"Cache-Control": "no-store"})


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
    days: int = Query(1, ge=0, le=366),
    date_from: str | None = None,
    date_to: str | None = None,
    force: bool = False,
):
    last = db.last_run()
    if not force and last:
        finished = datetime.fromisoformat(last["finished_at"])
        if datetime.now().astimezone() - finished < MIN_COLLECT_INTERVAL:
            return {"skipped": True, "last_run": last}

    since, until = period_datetimes(days, date_from, date_to)

    if not collect_lock.acquire(blocking=False):
        return {"skipped": True, "busy": True, "last_run": last}
    try:
        keywords = db.enabled_keywords()
        run_id = db.start_run()
        new_articles, keyword_hits, errors, print_updates = collector.collect(
            keywords, since, until, known_keys=db.known_article_keys()
        )
        new_count = db.save_articles(new_articles, keyword_hits)
        db.fill_print_pages(print_updates)
        db.finish_run(run_id, new_count, errors)
        return {"skipped": False, "new_count": new_count, "errors": errors, "last_run": db.last_run()}
    finally:
        collect_lock.release()


@app.get("/api/status")
def status():
    return {"last_run": db.last_run(), "version": VERSION, "archive": archive.info(),
            "categories": archive.CATEGORIES, "window_mode": WINDOW_MODE}


# ---------------- 창 모드: 프로그램 창이 모두 닫히면 서버도 끝낸다 ----------------

WINDOW_MODE = False         # launcher.py(GoodMorningKOITA.vbs)로 켰을 때 True
WINDOW_GRACE_SECONDS = 20   # 마지막 창이 닫힌 뒤 이만큼 기다렸다 끈다 (새로고침·창 이동 중 끊김 대비)
FIRST_WINDOW_SECONDS = 180  # 켠 뒤 창이 한 번도 안 붙으면 이만큼 뒤에 끈다
open_windows = 0
open_windows_lock = threading.Lock()


@app.get("/api/alive")
async def alive(request: Request):
    """창 모드에서 각 화면이 열려 있는 동안 붙잡고 있는 연결 (EventSource). 연결 수 = 열린 프로그램 창 수"""
    async def stream():
        global open_windows
        with open_windows_lock:
            open_windows += 1
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(5)
                yield ": ping\n\n"
        finally:
            with open_windows_lock:
                open_windows -= 1
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def watch_windows(server):
    """열린 프로그램 창이 0개로 WINDOW_GRACE_SECONDS 넘게 있으면 서버를 끝낸다 (검색·DB 저장 중이면 끝날 때까지 기다림)"""
    started = time.monotonic()
    seen = False
    empty_since = None
    while not server.should_exit:
        time.sleep(2)
        now = time.monotonic()
        if open_windows > 0:
            seen, empty_since = True, None
            continue
        if empty_since is None:
            empty_since = now
        waited = now - empty_since if seen else now - started
        if waited >= (WINDOW_GRACE_SECONDS if seen else FIRST_WINDOW_SECONDS) \
                and not collect_lock.locked() and not archive_lock.locked():
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 프로그램 창이 모두 닫혀 종료합니다.", flush=True)
            server.should_exit = True


# ---------------- 기사 ----------------

def period_datetimes(days, date_from, date_to):
    """
    모든 기간은 날짜(한국 시간 0시) 기준이다.
    days면 오늘 0시에서 days일 전 0시부터 지금까지 (0 = 오늘, 1 = 어제부터, 6 = 최근 7일),
    날짜를 주면 date_from 00:00 ~ date_to 다음 날 00:00 (끝 날짜 포함)
    """
    if not (date_from or date_to):
        today = datetime.now(collector.KST).replace(hour=0, minute=0, second=0, microsecond=0)
        return today - timedelta(days=days), None
    try:
        start = datetime.fromisoformat(date_from).replace(tzinfo=collector.KST) if date_from else None
        end = datetime.fromisoformat(date_to).replace(tzinfo=collector.KST) + timedelta(days=1) if date_to else None
    except ValueError:
        raise HTTPException(400, "날짜 형식이 올바르지 않습니다.")
    if start and end and start >= end:
        raise HTTPException(400, "시작 날짜가 끝 날짜보다 늦습니다.")
    # 시작 날짜만 없으면 끝 날짜 하루치
    return start or end - timedelta(days=1), end


def period(days, date_from, date_to):
    since, until = period_datetimes(days, date_from, date_to)
    as_text = lambda d: d.isoformat(timespec="seconds") if d else None
    return as_text(since), as_text(until)


@app.get("/api/articles")
def get_articles(days: int = 1, date_from: str | None = None, date_to: str | None = None):
    since, until = period(days, date_from, date_to)
    rows = db.list_articles(since, until)
    saved = archive.saved_categories()
    for row in rows:
        row["archived"] = row["url"] in saved
        # 목록에서 아직 안 골랐으면 DB(편집기 등)에 들어 있는 분류를 보여 준다
        if not row["category"] and saved.get(row["url"]):
            row["category"] = saved[row["url"]]
    return rows


class ArticlePatch(BaseModel):
    press: str | None = None
    print_page: str | None = None
    has_video: bool | None = None
    category: str | None = None


@app.patch("/api/articles/{article_id}")
def patch_article(article_id: int, body: ArticlePatch):
    if body.category and body.category not in archive.CATEGORIES:
        raise HTTPException(400, f"분류는 {', '.join(archive.CATEGORIES)} 중 하나여야 합니다.")
    db.update_article(article_id, body.press, body.print_page, body.has_video, body.category)
    return {"ok": True}


class CategorySelection(BaseModel):
    ids: list[int]
    category: str


@app.post("/api/articles/category")
def set_categories(body: CategorySelection):
    """체크한 기사 여러 개의 분류를 한 번에 고른다 (빈 값 = 선택으로 되돌리기)"""
    if body.category and body.category not in archive.CATEGORIES:
        raise HTTPException(400, f"분류는 {', '.join(archive.CATEGORIES)} 중 하나여야 합니다.")
    if not body.ids:
        raise HTTPException(400, "체크한 기사가 없습니다.")
    db.set_category(body.ids, body.category)
    return {"ok": True, "count": len(body.ids)}


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


# ---------------- 기사 DB 편집기 ----------------

@app.get("/api/archive/rows")
def archive_rows():
    """기사 DB 전체 행과 지문(revision). 저장할 때 이 지문을 같이 보낸다"""
    return archive.editor_data()


class ArchiveEdit(BaseModel):
    revision: str
    rows: list[dict]


@app.put("/api/archive/rows")
def replace_archive_rows(body: ArchiveEdit):
    """편집기에서 고친 전체 행으로 기사 DB(SQLite)를 바꾸고 CSV·JSON도 똑같이 다시 만든다"""
    with archive_lock:
        try:
            result = archive.replace_all(body.rows, body.revision)
        except archive.EditError as exc:
            status = 422 if exc.problems else 409
            return JSONResponse({"detail": str(exc), "problems": exc.problems}, status_code=status)
    return result | archive.editor_data()


class PrintCheck(BaseModel):
    folder: str | None = None       # 서버(이 프로그램이 도는 PC)의 폴더. 비우면 .env NEWSPAPER_DIR
    names: list[str] | None = None  # 브라우저에서 고른 폴더의 파일명 (폰·클라우드 서버에서 쓸 때)
    rows: list[dict] | None = None  # 편집기의 지금 행 (저장 전 수정 반영). 비우면 저장된 DB


@app.get("/api/print-check/folder")
def print_check_folder():
    return {"folder": printcheck.default_folder()}


@app.post("/api/print-check")
def print_check(body: PrintCheck):
    """지면 기사 파일명과 기사 DB 지면 행의 날짜·지면을 비교한다 (DB는 고치지 않음)"""
    folder = (body.folder or "").strip() or printcheck.default_folder()
    if body.names is not None:
        names, source = body.names, "선택한 폴더"
    else:
        if not folder:
            raise HTTPException(400, "지면 기사 폴더를 입력하세요 (.env의 NEWSPAPER_DIR에 넣어 두면 기본값이 됩니다).")
        try:
            names, source = printcheck.list_files(folder), folder
        except FileNotFoundError as exc:
            raise HTTPException(404, f"{exc} (클라우드 서버에서 쓰는 중이면 '폴더 직접 고르기'를 쓰세요.)")
    rows = body.rows if body.rows is not None else archive.all_rows()
    return printcheck.compare(names, rows, printcheck.load_links()) | {"source": source}


@app.get("/api/print-check/file")
def print_check_file(name: str, folder: str | None = None):
    """지면 확인 결과에서 파일명을 누르면 그 지면 기사 파일(JPG 등)을 브라우저 새 탭에 연다"""
    folder = (folder or "").strip() or printcheck.default_folder()
    try:
        path = printcheck.file_path(folder, name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    # inline: 내려받지 않고 브라우저에서 바로 보여 준다
    return FileResponse(path, headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(path.name)}"})


class PrintTrash(BaseModel):
    name: str                  # 지면 확인 결과의 파일 (폴더 안 상대 경로)
    folder: str | None = None  # 비우면 .env NEWSPAPER_DIR


@app.post("/api/print-check/trash")
def print_check_trash(body: PrintTrash):
    """실수로 저장한 지면 기사 파일을 폴더 안 '_삭제한 파일' 폴더로 옮긴다 (완전히 지우지 않음, 다음 비교부터 빠짐)"""
    folder = (body.folder or "").strip() or printcheck.default_folder()
    if not folder:
        raise HTTPException(400, "지면 기사 폴더가 없습니다.")
    try:
        moved = printcheck.trash_file(folder, body.name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "moved_to": str(moved)}


class PrintLink(BaseModel):
    file: str
    press: str | None = None  # 비우면 연결 끊기
    title: str | None = None


class TitleAlias(BaseModel):
    press: str
    old: str
    new: str


@app.post("/api/archive/title-alias")
def archive_title_alias(body: TitleAlias):
    """
    지면 확인에서 지면 행 제목을 지면 기사 제목으로 바꿀 때 부른다 (표의 제목은 화면에서 바꾸고 저장해야 DB에 들어감).
    기사 목록에서 같은 기사를 다시 DB 저장해도 예전 제목의 지면 행이 또 생기지 않게 기록하고, 파일 연결도 새 제목으로 옮긴다.
    """
    if not (body.press.strip() and body.old.strip() and body.new.strip()):
        raise HTTPException(400, "언론사명과 바꾸기 전·후 제목이 필요합니다.")
    archive.add_title_alias(body.press, body.old, body.new)
    printcheck.retitle_links(body.press, body.old, body.new)
    return {"ok": True}


@app.post("/api/print-check/link")
def print_check_link(body: PrintLink):
    """지면 기사 파일을 DB 지면 행(언론사명·제목)에 직접 연결해 기억한다. 다음 확인부터 그 행과 비교"""
    if body.press is not None and not (body.press.strip() and (body.title or "").strip()):
        raise HTTPException(400, "연결할 DB 행의 언론사명·제목이 필요합니다.")
    printcheck.save_link(body.file, body.press, body.title)
    return {"ok": True}


@app.post("/api/export")
def export_selected(body: Selection):
    """체크한 기사만 엑셀로"""
    return xlsx_response(selected_articles(body))


@app.get("/api/export")
def export(days: int = 1, date_from: str | None = None, date_to: str | None = None):
    """기간 전체를 엑셀로 (v1.0.0 방식, 주소로 바로 받을 때)"""
    since, until = period(days, date_from, date_to)
    return xlsx_response(db.list_articles(since, until))


def xlsx_response(articles):
    content = exporter.build_workbook(articles)
    filename = f"산기협_뉴스_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def serve(window_mode=False):
    """
    서버 실행. window_mode=True(launcher.py): 크롬에 프로그램 화면(탭)을 열고, 그 탭이 모두 닫히면 끝난다.
    False(run.bat·클라우드): 예전처럼 CMD 창에서 돈다
    """
    global WINDOW_MODE
    import uvicorn

    # .env의 HOST·PORT로 바꿀 수 있다. 클라우드 서버에서는 HOST=0.0.0.0
    host = os.getenv("HOST", "").strip() or "127.0.0.1"
    port = int(os.getenv("PORT", "").strip() or 8000)
    url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips="*"))
    WINDOW_MODE = window_mode
    if window_mode:
        import window
        threading.Thread(target=watch_windows, args=(server,), daemon=True).start()
        threading.Timer(1.5, window.open_window, [url]).start()
    # 내 PC에서 켤 때만 브라우저를 자동으로 연다 (OPEN_BROWSER=0이면 안 연다)
    elif os.getenv("OPEN_BROWSER", "").strip() != "0" and host in ("127.0.0.1", "localhost"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server.run()


if __name__ == "__main__":
    serve()
