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
ARTICLE_COLUMNS = ["날짜", "언론사", "구분", "제목", "URL"]
ARTICLE_WIDTHS = [13, 18, 12, 70, 50]
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
    왼쪽 보도자료 칸(배포일·보도자료명·건수)은 그 줄들을 세로로 합친다. 오른쪽에 기사 날짜·언론사·구분·제목·URL.
    URL이 http로 시작하면(온라인·방송) 누르면 열리는 링크. 연결된 기사가 없는 보도자료도 한 줄로 남긴다
    """
    return grouped_articles_workbook("보도자료별 연결 기사", RELEASE_COLUMNS, RELEASE_WIDTHS[:2] + [6, 6, 6, 6],
                                     [(release_values(r), r["articles"]) for r in releases], text_col=2)


def grouped_articles_workbook(title, left_columns, left_widths, groups, text_col):
    """
    groups: [(왼쪽 칸 값들, 기사 목록)]. 기사 수만큼 줄을 쓰고 왼쪽 칸은 세로로 합친다.
    오른쪽 기사 칸: 날짜·언론사·구분·제목·URL (URL이 http면 링크). text_col: 왼쪽에서 왼쪽 정렬·줄바꿈할 열 번호(1부터)
    """
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(left_columns + ARTICLE_COLUMNS)
    n = len(left_columns)
    for left_values, group_articles in groups:
        start = ws.max_row + 1
        articles = group_articles or [None]
        for i, a in enumerate(articles):
            left = left_values if i == 0 else [None] * n
            right = [a["date"], a["press"], a["type"], a["title"], a["url"]] if a else [None] * len(ARTICLE_COLUMNS)
            ws.append(left + right)
            url_cell = ws.cell(ws.max_row, n + len(ARTICLE_COLUMNS))
            if isinstance(url_cell.value, str) and url_cell.value.startswith(("http://", "https://")):
                url_cell.hyperlink = url_cell.value
                url_cell.style = "Hyperlink"
        end = ws.max_row
        for col in range(1, n + 1):
            if end > start:
                ws.merge_cells(start_row=start, end_row=end, start_column=col, end_column=col)
            ws.cell(start, col).alignment = Alignment(
                horizontal="left" if col == text_col else "center", vertical="center", wrap_text=col == text_col)
        for col in range(1, n + len(ARTICLE_COLUMNS) + 1):
            ws.cell(start, col).border = GROUP_TOP
    style_header(ws, left_widths + ARTICLE_WIDTHS)
    for row in ws.iter_rows(min_row=2, min_col=n + 1, max_col=n + 3):
        for cell in row:
            cell.alignment = Alignment(horizontal="center", vertical="center")
    return to_bytes(wb)


# ---------------- 특별보도 엑셀 (v1.5.0) ----------------

SPECIAL_COLUMNS = ["날짜", "구분", "특별보도명", "기사 행", "온라인", "지면", "방송"]
SPECIAL_WIDTHS = [13, 10, 60, 8, 7, 7, 7]


def build_special(groups):
    """특별보도 묶음 1건마다 묶인 기사 전체 (특별보도 칸은 세로로 합침). 맨 아래 구분별 건수는 별도 시트"""
    data = grouped_articles_workbook(
        "특별보도", SPECIAL_COLUMNS, SPECIAL_WIDTHS,
        [([g["date"], g["subtype"], g["title"], g["counts"]["total"], g["counts"]["online"],
           g["counts"]["print"], g["counts"]["broadcast"]], g["articles"]) for g in groups], text_col=3)
    from openpyxl import load_workbook
    wb = load_workbook(BytesIO(data))
    ws = wb.create_sheet("구분별 건수")
    ws.append(["구분", "건수(묶음)", "기사 행", "온라인", "지면", "방송"])
    subtypes = list(dict.fromkeys(["기고", "인터뷰", "기획보도"] + [g["subtype"] for g in groups]))
    for s in subtypes:
        mine = [g for g in groups if g["subtype"] == s]
        ws.append([s, len(mine)] + [sum(g["counts"][k] for g in mine) for k in ("total", "online", "print", "broadcast")])
    ws.append(["합계", len(groups)] + [sum(g["counts"][k] for g in groups) for k in ("total", "online", "print", "broadcast")])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    style_header(ws, [12, 12, 10, 8, 8, 8])
    return to_bytes(wb)


# ---------------- 기사 DB 엑셀 (DB 편집 창, v1.5.0) ----------------

DB_COLUMNS = [("seq", "연번", 8), ("date", "날짜", 13), ("press", "언론사명", 18), ("type", "구분", 12),
              ("title", "제목", 70), ("url", "URL", 50), ("published_at", "게재일시", 22),
              ("keywords", "키워드", 20), ("saved_at", "저장일시", 22), ("category", "분류", 12)]


def build_db_rows(rows):
    """기사 DB 행 그대로 (DB와 같은 열 순서)"""
    wb = Workbook()
    ws = wb.active
    ws.title = "기사 DB"
    ws.append([h for _, h, _ in DB_COLUMNS])
    for r in rows:
        ws.append([r.get(n) if r.get(n) is not None else "" for n, _, _ in DB_COLUMNS])
        url_cell = ws.cell(ws.max_row, 6)
        if isinstance(url_cell.value, str) and url_cell.value.startswith(("http://", "https://")):
            url_cell.hyperlink = url_cell.value
            url_cell.style = "Hyperlink"
    style_header(ws, [w for _, _, w in DB_COLUMNS])
    if rows:
        ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for i in (0, 1, 3, 9):
            row[i].alignment = Alignment(horizontal="center", vertical="center")
    return to_bytes(wb)
