"""
엑셀 내보내기 (기존 naver_news_crawler10.py와 같은 규칙)

- 모든 기사: 온라인 1행 (날짜 = 온라인 게재일, URL = 기사 URL)
- 영상 기사: 방송 1행 추가 (날짜·URL은 온라인과 동일)
- 지면 기사: 지면 1행 추가 (날짜 = 온라인 게재일 + 1일, URL = '-')
- 정렬: 날짜 내림차순, 같은 날짜는 지면 → 방송 → 온라인
"""

from datetime import datetime, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

COLUMNS = ["연번", "날짜", "언론사명", "구분", "제목", "URL"]
WIDTHS = [8, 15, 20, 12, 70, 70]


def row_key(r):
    """같은 행인지 가리는 키. 온라인·방송은 (URL, 구분), 지면은 (날짜, 언론사명, 구분, 제목)"""
    if r["구분"] in ("온라인", "방송"):
        return "|".join(("d", r["URL"], r["구분"]))
    return "|".join(("p", r["날짜"], r["언론사명"], r["구분"], r["제목"]))


def type_order(r):
    """같은 날짜 안에서 지면 → 방송 → 온라인"""
    return 1 if r["구분"] == "방송" else 2 if r["구분"] == "온라인" else 0


def to_rows(articles):
    rows = []
    for a in articles:
        online_date = datetime.fromisoformat(a["published_at"])
        date_text = online_date.strftime("%Y.%m.%d.")
        press = a.get("press") or ""
        url = a.get("url") or a.get("naver_link") or a.get("original_link")
        # 엑셀에는 안 나가고 DB 저장에만 쓰는 값
        extra = {"게재일시": a["published_at"], "키워드": a.get("keywords") or "", "분류": a.get("category") or ""}

        if a.get("print_page") and a["print_page"] != "온라인":
            rows.append({
                "날짜": (online_date + timedelta(days=1)).strftime("%Y.%m.%d."),
                "언론사명": press, "구분": a["print_page"], "제목": a["title"], "URL": "-", **extra,
            })
        if a.get("has_video"):
            rows.append({"날짜": date_text, "언론사명": press, "구분": "방송", "제목": a["title"], "URL": url, **extra})
        rows.append({"날짜": date_text, "언론사명": press, "구분": "온라인", "제목": a["title"], "URL": url, **extra})

    seen = set()
    unique = []
    for r in rows:
        key = row_key(r)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda r: (-int(r["날짜"].replace(".", "")), type_order(r), r["언론사명"], r["제목"]))
    for index, r in enumerate(unique, start=1):
        r["연번"] = index
    return unique


def build_workbook(articles):
    wb = Workbook()
    ws = wb.active
    ws.title = "뉴스"
    ws.append(COLUMNS)

    for r in to_rows(articles):
        ws.append([r[c] for c in COLUMNS])

    for letter, width in zip("ABCDEF", WIDTHS):
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in ws.iter_rows(min_row=2):
        for cell in row[:4]:
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for cell in row[4:]:
            cell.alignment = Alignment(vertical="center")
        url_cell = row[5]
        if isinstance(url_cell.value, str) and url_cell.value.startswith(("http://", "https://")):
            url_cell.hyperlink = url_cell.value
            url_cell.style = "Hyperlink"

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# ---------------- 보도자료 엑셀 (v1.5.0) ----------------
# pressrelease.overview()의 releases를 받는다. 건수는 DB 행 기준 (온라인·지면·방송 각각 1건)

RELEASE_COLUMNS = ["배포일", "보도자료명", "총", "온라인", "지면", "방송"]
RELEASE_WIDTHS = [13, 70, 8, 8, 8, 8]
ARTICLE_COLUMNS = ["날짜", "언론사", "제목", "URL"]
ARTICLE_WIDTHS = [13, 28, 70, 50]
HEADER_FILL = PatternFill("solid", fgColor="E8EEFC")
THIN = Side(style="thin", color="D0D5DD")
GROUP_TOP = Border(top=THIN)


def release_name(r):
    """엑셀에 쓰는 보도자료명: [종류]를 앞에 붙인다 (예: [보도자료] 2026년 하반기 ...)"""
    return f"[{r['kind']}] {r['title']}" if r.get("kind") else r["title"]


def release_values(r):
    c = r["counts"]
    return [r["date"], release_name(r), c["total"], c["online"], c["print"], c["broadcast"]]


def style_header(ws, widths):
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"


def to_bytes(wb):
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_release_summary(releases):
    """엑셀 1: 보도자료 목록 + 보도자료별 보도 건수 (맨 아래 합계·1건당 평균)"""
    wb = Workbook()
    ws = wb.active
    ws.title = "보도자료별 보도건수"
    ws.append(RELEASE_COLUMNS)
    for r in releases:
        ws.append(release_values(r))
    last = ws.max_row
    if releases:
        ws.auto_filter.ref = f"A1:F{last}"
        ws.append([])
        sums = [sum(r["counts"][k] for r in releases) for k in ("total", "online", "print", "broadcast")]
        ws.append(["합계", f"보도자료 {len(releases)}건", *sums])
        ws.append(["", "보도자료 1건당 평균 보도건수", round(sums[0] / len(releases), 1)])
        for row in ws.iter_rows(min_row=last + 2):
            for cell in row:
                cell.font = Font(bold=True)
    style_header(ws, RELEASE_WIDTHS)
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(horizontal="center", vertical="center")
        for cell in row[2:6]:
            cell.alignment = Alignment(horizontal="center", vertical="center")
    return to_bytes(wb)


def build_release_articles(releases):
    """
    엑셀 2: 보도자료에 연결된 기사 전체. 보도자료 1건의 기사 수만큼 줄을 쓰고,
    왼쪽 보도자료 칸(배포일·보도자료명·건수)은 그 줄들을 세로로 합친다. 오른쪽에 기사 날짜·언론사(언론사명 구분)·제목·URL.
    URL이 http로 시작하면(온라인·방송) 누르면 열리는 링크. 연결된 기사가 없는 보도자료도 한 줄로 남긴다
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "보도자료별 연결 기사"
    ws.append(RELEASE_COLUMNS + ARTICLE_COLUMNS)
    for r in releases:
        start = ws.max_row + 1
        articles = r["articles"] or [None]
        for a in articles:
            left = release_values(r) if a is articles[0] else [None] * len(RELEASE_COLUMNS)
            right = [a["date"], f"{a['press']} {a['type']}".strip(), a["title"], a["url"]] if a else [None] * 4
            ws.append(left + right)
            url_cell = ws.cell(ws.max_row, len(RELEASE_COLUMNS) + 4)
            if isinstance(url_cell.value, str) and url_cell.value.startswith(("http://", "https://")):
                url_cell.hyperlink = url_cell.value
                url_cell.style = "Hyperlink"
        end = ws.max_row
        for col in range(1, len(RELEASE_COLUMNS) + 1):
            if end > start:
                ws.merge_cells(start_row=start, end_row=end, start_column=col, end_column=col)
            ws.cell(start, col).alignment = Alignment(
                horizontal="left" if col == 2 else "center", vertical="center", wrap_text=col == 2)
        for col in range(1, len(RELEASE_COLUMNS) + len(ARTICLE_COLUMNS) + 1):
            ws.cell(start, col).border = GROUP_TOP
    style_header(ws, RELEASE_WIDTHS[:2] + [6, 6, 6, 6] + ARTICLE_WIDTHS)
    for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
        row[0].alignment = Alignment(horizontal="center", vertical="center")
    return to_bytes(wb)
