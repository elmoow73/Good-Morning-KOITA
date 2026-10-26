"""
네이버 뉴스 수집 엔진

1) NAVER API HUB 뉴스 검색 API로 키워드별 최신 기사를 받아 기준 시간 이내 기사만 남긴다.
2) 새로 발견한 기사만 기사 페이지를 열어 언론사명, 지면, 영상 여부를 보강한다.
   (크롬 없이 requests로 HTML만 받아서 판별)
"""

import html
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

# 2026년 네이버 개발자센터 검색 API가 NAVER API HUB로 이관됨
# https://api.ncloud-docs.com/docs/naver-api-hub-search-news
API_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
API_DISPLAY = 100      # 한 번에 받을 수 있는 최대 개수
API_MAX_START = 1000   # 네이버 API start 최대값

PAGE_TIMEOUT = 10
ENRICH_WORKERS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


# =========================================================
# 영상 확인 대상 매체 (기존 스크립트와 동일)
# =========================================================

VIDEO_MEDIA_ALIASES = {
    "KBS": {"KBS", "KBS NEWS", "KBS뉴스"},
    "SBS": {"SBS", "SBS NEWS", "SBS뉴스"},
    "MBC": {"MBC", "MBC NEWS", "MBC뉴스"},
    "TV조선": {"TV조선", "TV CHOSUN", "TVCHOSUN", "티비조선"},
    "채널A": {"채널A", "CHANNEL A", "CHANNELA"},
    "JTBC": {"JTBC", "JTBC NEWS", "JTBC뉴스"},
    "MBN": {"MBN", "MBN NEWS", "MBN뉴스", "매일방송"},
    "한국경제TV": {"한국경제TV", "한국경제 TV", "한국경제티브이", "WOWTV", "WOW TV"},
    "YTN": {"YTN", "YTN NEWS", "YTN뉴스"},
    "연합뉴스TV": {"연합뉴스TV", "연합뉴스 TV", "연합뉴스티브이", "YONHAP NEWS TV", "YONHAPNEWSTV"},
    "YTN사이언스": {"YTN사이언스", "YTN 사이언스", "YTN SCIENCE", "YTNSCIENCE", "사이언스TV"},
}

# og:site_name이 없거나 영문일 때 쓰는 도메인 → 언론사명
DOMAIN_PRESS = {
    "yna.co.kr": "연합뉴스",
    "yonhapnewstv.co.kr": "연합뉴스TV",
    "newsis.com": "뉴시스",
    "news1.kr": "뉴스1",
    "chosun.com": "조선일보",
    "joongang.co.kr": "중앙일보",
    "donga.com": "동아일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "hankookilbo.com": "한국일보",
    "kmib.co.kr": "국민일보",
    "seoul.co.kr": "서울신문",
    "segye.com": "세계일보",
    "munhwa.com": "문화일보",
    "mk.co.kr": "매일경제",
    "hankyung.com": "한국경제",
    "sedaily.com": "서울경제",
    "mt.co.kr": "머니투데이",
    "edaily.co.kr": "이데일리",
    "asiae.co.kr": "아시아경제",
    "fnnews.com": "파이낸셜뉴스",
    "heraldcorp.com": "헤럴드경제",
    "etnews.com": "전자신문",
    "dt.co.kr": "디지털타임스",
    "zdnet.co.kr": "지디넷코리아",
    "ddaily.co.kr": "디지털데일리",
    "inews24.com": "아이뉴스24",
    "dongascience.com": "동아사이언스",
    "hellodd.com": "헬로디디",
    "kbs.co.kr": "KBS",
    "imbc.com": "MBC",
    "sbs.co.kr": "SBS",
    "jtbc.co.kr": "JTBC",
    "joins.com": "JTBC",
    "mbn.co.kr": "MBN",
    "tvchosun.com": "TV조선",
    "ichannela.com": "채널A",
    "ytn.co.kr": "YTN",
    "science.ytn.co.kr": "YTN사이언스",
    "wowtv.co.kr": "한국경제TV",
}

VIDEO_HTML_PATTERNS = [
    r"<video[\s>]",
    r'property=["\']og:video',
    r'name=["\']twitter:player',
    r'https?[^"\'\s]+\.mp4',
    r'https?[^"\'\s]+\.m3u8',
    r'"contenturl"\s*:\s*"[^"]+',
    r'"embedurl"\s*:\s*"[^"]+',
    r'"@type"\s*:\s*"videoobject"',
    r'<iframe[^>]+src=["\'][^"\']*(youtube\.com|youtu\.be|tv\.naver\.com|tv\.kakao\.com|vod|player)',
    r'class=["\'][^"\']*(vod_player|video_player|video-player|vod-player|_VIDEO_AREA|jwplayer|video-js)',
]


# =========================================================
# 공통 유틸
# =========================================================

def normalize_media_name(name):
    if not name:
        return ""
    return re.sub(r"[\s·ㆍ_\-]+", "", str(name).strip().upper())


def get_standard_video_media_name(press_name):
    target = normalize_media_name(press_name)
    if not target:
        return ""
    for standard_name, aliases in VIDEO_MEDIA_ALIASES.items():
        if target in {normalize_media_name(a) for a in aliases}:
            return standard_name
    return ""


def find_print_page(text):
    """'A29면 1단' -> 'A29면'. 없으면 '온라인'."""
    if not text:
        return "온라인"
    match = re.search(r"(?<![가-힣A-Za-z0-9])([A-Za-z]?\d+)\s*면", text)
    if not match:
        return "온라인"
    return f"{match.group(1).upper()}면"


def strip_tags(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def domain_of(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def press_from_domain(url):
    host = domain_of(url)
    # 가장 긴 도메인부터 비교해서 science.ytn.co.kr이 ytn.co.kr보다 먼저 맞도록
    for domain in sorted(DOMAIN_PRESS, key=len, reverse=True):
        if host == domain or host.endswith("." + domain):
            return DOMAIN_PRESS[domain]
    return ""


def is_naver_news_link(url):
    host = domain_of(url)
    return host.endswith("news.naver.com")


# =========================================================
# 1) 네이버 검색 API
# =========================================================

class NaverApiError(Exception):
    pass


class IncompleteSearch(Exception):
    """API가 돌려줄 수 있는 최대 건수에 닿아 기간 앞부분을 다 받지 못했을 때"""

    def __init__(self, results):
        super().__init__("API가 키워드당 최신 1,100건까지만 제공해서 기간 앞쪽 기사 일부가 빠졌을 수 있습니다.")
        self.results = results


# NAVER API HUB(네이버 클라우드 플랫폼) 상태 코드별 안내
API_STATUS_HINTS = {
    401: "인증 실패: .env의 Client ID / Secret이 맞는지, API HUB Application에 '뉴스 검색' API가 등록돼 있는지 확인하세요.",
    403: "호출이 허용되지 않았습니다.",
    429: "하루 호출 한도(25,000회)를 넘었습니다.",
}


def get_credentials():
    client_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise NaverApiError(".env 파일에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET이 없습니다.")
    return client_id, client_secret


def api_error_message(response):
    """API Gateway 오류({"error": {...}})와 검색 API 오류({"errorCode", "errorMessage"}) 모두 처리"""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body.get("error"), dict):
        error = body["error"]
        detail = " / ".join(filter(None, [error.get("message"), error.get("details")]))
    else:
        detail = " ".join(filter(None, [body.get("errorCode"), body.get("errorMessage")]))
    detail = detail or response.text[:200]
    hint = API_STATUS_HINTS.get(response.status_code)
    return f"네이버 API 오류 ({response.status_code}): {detail}" + (f" → {hint}" if hint else "")


def search_keyword(keyword, since, until=None, session=None):
    """키워드를 최신순으로 검색해 since 이상, until 미만에 발행된 기사만 돌려준다."""
    client_id, client_secret = get_credentials()
    session = session or requests.Session()
    headers = {
        "X-NCP-APIGW-API-KEY-ID": client_id,
        "X-NCP-APIGW-API-KEY": client_secret,
    }

    results = []
    start = 1
    while start <= API_MAX_START:
        response = session.get(
            API_URL,
            headers=headers,
            params={"query": keyword, "display": API_DISPLAY, "start": start, "sort": "date", "format": "json"},
            timeout=PAGE_TIMEOUT,
        )
        if response.status_code != 200:
            raise NaverApiError(api_error_message(response))

        items = response.json().get("items", [])
        if not items:
            break

        reached_old = False
        for item in items:
            published = parsedate_to_datetime(item["pubDate"]).astimezone(KST)
            if published < since:
                reached_old = True
                continue
            if until and published >= until:
                continue
            naver_link = item.get("link", "")
            original_link = item.get("originallink", "") or naver_link
            results.append({
                "keyword": keyword,
                "title": strip_tags(item.get("title")),
                "description": strip_tags(item.get("description")),
                "naver_link": naver_link if is_naver_news_link(naver_link) else "",
                "original_link": original_link,
                "published_at": published.isoformat(timespec="seconds"),
            })

        # 최신순이므로 기준 시간보다 오래된 기사가 나오면 더 볼 필요가 없다
        if reached_old or len(items) < API_DISPLAY:
            break
        start += API_DISPLAY
    else:
        # API는 키워드당 최신 1,100건까지만 주므로 그보다 오래된 기사는 받을 수 없다
        raise IncompleteSearch(results)

    return results


def article_key(article):
    """같은 기사를 가리키는 고유 키. 네이버뉴스 링크가 있으면 그걸 우선."""
    return article.get("naver_link") or article.get("original_link")


# =========================================================
# 2) 기사 페이지 보강 (언론사명·지면·영상)
# =========================================================

def fetch_html(url, session):
    try:
        response = session.get(url, headers=HEADERS, timeout=PAGE_TIMEOUT)
        if response.status_code != 200:
            return ""
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding
        return response.text
    except requests.RequestException:
        return ""


def parse_naver_article(page_html):
    """네이버뉴스 기사 페이지에서 언론사명과 지면 정보를 읽는다."""
    soup = BeautifulSoup(page_html, "html.parser")
    press = ""

    meta = soup.select_one('meta[property="og:article:author"]')
    if meta and meta.get("content"):
        press = meta["content"].split("|")[0].strip()

    if not press:
        logo = soup.select_one(".media_end_head_top_logo img[alt], .media_end_head_top_logo_img[alt]")
        if logo:
            press = logo.get("alt", "").strip()

    # 지면 기사는 기사 상단에 "A12면 1단" 같은 표시가 붙는다
    print_page = "온라인"
    for element in soup.select(
        "[class*='newspaper'], [class*='paper'], .media_end_head_info, .media_end_head_info_datestamp"
    ):
        candidate = find_print_page(element.get_text(" ", strip=True))
        if candidate != "온라인":
            print_page = candidate
            break

    return press, print_page


def parse_original_press(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    for selector in ('meta[property="og:site_name"]', 'meta[name="application-name"]'):
        meta = soup.select_one(selector)
        if meta and meta.get("content"):
            name = meta["content"].strip()
            if re.search(r"[가-힣A-Za-z]", name) and len(name) <= 30:
                return name
    return ""


def has_video_html(page_html):
    lowered = (page_html or "").lower()
    return any(re.search(pattern, lowered) for pattern in VIDEO_HTML_PATTERNS)


def enrich_article(article, session=None):
    """기사 1건에 press / print_page / has_video를 채운다."""
    session = session or requests.Session()
    press = ""
    print_page = "온라인"
    naver_html = ""
    original_html = ""

    if article.get("naver_link"):
        naver_html = fetch_html(article["naver_link"], session)
        if naver_html:
            press, print_page = parse_naver_article(naver_html)

    if not press:
        press = press_from_domain(article["original_link"])

    if not press:
        original_html = fetch_html(article["original_link"], session)
        press = parse_original_press(original_html) or domain_of(article["original_link"])

    has_video = False
    if get_standard_video_media_name(press) or get_standard_video_media_name(
        press_from_domain(article["original_link"])
    ):
        if not original_html:
            original_html = fetch_html(article["original_link"], session)
        has_video = has_video_html(original_html) or has_video_html(naver_html)

    article.update({"press": press, "print_page": print_page, "has_video": has_video})
    return article


def enrich_articles(articles):
    if not articles:
        return articles
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        list(pool.map(lambda a: enrich_article(a, session), articles))
    return articles


# =========================================================
# 3) 지면 표시 (네이버 뉴스 검색 결과 화면)
# =========================================================
#
# 뉴스 검색 API는 지면 정보를 주지 않는다. 네이버 뉴스 검색 결과 화면에는 언론사와 상관없이
# 기사마다 "A29면 1단" 같은 표시가 같은 형식으로 붙으므로(예전 naver_news_crawler10.py 방식),
# 같은 키워드로 검색 결과 화면을 브라우저(Playwright)로 열어 기사 링크별 지면을 읽는다.

SEARCH_PAGE_URL = "https://search.naver.com/search.naver"
SEARCH_MAX_SCROLLS = 200      # 키워드당 아래로 내리는 최대 횟수 (기간 기사가 다 나오면 그 전에 멈춤)
SEARCH_SCROLL_WAIT_MS = 3000  # 내린 뒤 새 기사를 기다리는 최대 시간 (나타나면 바로 넘어감)
SEARCH_STOP_REPEATS = 2       # 이만큼 연속으로 기다려도 안 늘어나면 끝까지 다 나온 것으로 본다
SEARCH_PRINT_RE = re.compile(r"(?<![가-힣A-Za-z0-9])([A-Za-z]?\d{1,3})\s*면\s*\d{1,2}\s*단")
SEARCH_TITLE_SELECTOR = "a[data-heatmap-target='.tit'], a.news_tit"


def normalize_url(url):
    """http/https, www., 끝의 / 차이를 무시하고 비교하려고"""
    parsed = urlparse((url or "").strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}" + (f"?{parsed.query}" if parsed.query else "")


def parse_search_print_pages(page_html):
    """
    검색 결과 화면 HTML에서 지면 기사를 찾는다.
    반환: ({기사 링크 또는 "제목:" + 정리한 제목: 지면}, 화면의 기사 수)
    링크가 API 링크와 모양이 달라 안 맞을 때를 위해 제목으로도 찾을 수 있게 넣어 둔다.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    titles = soup.select(SEARCH_TITLE_SELECTOR)
    pages = {}
    for title in titles:
        # 제목에서 위로 올라가며 이 기사 한 건을 감싸는 가장 큰 상자를 찾는다 (다른 기사 제목이 섞이기 직전까지)
        box = title
        for _ in range(15):
            parent = box.parent
            if parent is None or len(parent.select(SEARCH_TITLE_SELECTOR)) > 1:
                break
            box = parent
        match = SEARCH_PRINT_RE.search(box.get_text(" ", strip=True))
        if match:
            page = f"{match.group(1).upper()}면"
            pages[normalize_url(title.get("href"))] = page
            pages["제목:" + title_key(title.get_text(" ", strip=True))] = page
    return pages, len(titles)


def title_key(title):
    """띄어쓰기·기호를 뺀 제목 (검색 화면 제목과 API 제목을 맞출 때만 쓴다)"""
    title = re.sub(r"새\s*창\s*열림", "", title or "")
    return re.sub(r"[^0-9a-z가-힣]", "", title.lower())


def open_browser(playwright):
    """PC에 있는 크롬 → 엣지 → Playwright 크로미움 순서로 연다. .env의 BROWSER_SHOW=1이면 창을 띄운다."""
    headless = os.getenv("BROWSER_SHOW", "").strip() != "1"
    for channel in ("chrome", "msedge", None):
        try:
            return playwright.chromium.launch(channel=channel, headless=headless) if channel \
                else playwright.chromium.launch(headless=headless)
        except Exception:  # 그 브라우저가 설치돼 있지 않음
            continue
    raise RuntimeError("크롬이나 엣지를 찾지 못했습니다. 크롬을 설치하거나 'python -m playwright install chromium'을 실행하세요.")


def search_print_pages(keywords, since, until=None):
    """
    키워드를 OR로 묶은 네이버 뉴스 검색 결과 화면(최신순)을 브라우저로 열어 지면 기사 링크를 모은다.
    검색 화면은 자바스크립트로 기사 목록을 그리므로 HTML만 받아서는 읽을 수 없다. (예전 크롤러와 같은 방식)
    화면에서 검색한 기간과 똑같은 기간으로 네이버를 검색하고, 그 기간 기사가 끝까지 나올 때까지 내린다.
    반환: ({기사 링크: 지면}, 안내·오류 목록)
    """
    from playwright.sync_api import sync_playwright

    # until은 끝 날짜 다음 날 0시이므로 1초 빼서 끝 날짜로
    last_day = (until - timedelta(seconds=1)) if until else datetime.now(KST)
    period = f"p:from{since.astimezone(KST).strftime('%Y%m%d')}to{last_day.astimezone(KST).strftime('%Y%m%d')}"
    # 키워드를 '|'(네이버 검색의 OR)로 묶어 한 번만 검색한다. 키워드마다 따로 내리면 5배 걸린다
    query = " | ".join(keywords)
    pages, errors = {}, []
    with sync_playwright() as playwright:
        browser = open_browser(playwright)
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="ko-KR")
        try:
            page.goto(
                f"{SEARCH_PAGE_URL}?ssc=tab.news.all&where=news&query={quote(query)}"
                f"&sm=tab_opt&sort=1&nso={quote('so:dd,' + period)}",
                wait_until="domcontentloaded", timeout=PAGE_TIMEOUT * 1000,
            )
            try:
                page.wait_for_selector(SEARCH_TITLE_SELECTOR, timeout=SEARCH_SCROLL_WAIT_MS * 2)
            except Exception:
                pass  # 기간에 기사가 없으면 제목이 안 나온다
            # 맨 아래로 내리고, 새 기사가 나타나면 바로 다음으로 넘어간다.
            # 기다려도 안 늘어나는 일이 SEARCH_STOP_REPEATS번 연속이면 기간 기사가 다 나온 것으로 본다
            count, same = page.locator(SEARCH_TITLE_SELECTOR).count(), 0
            for _ in range(SEARCH_MAX_SCROLLS):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.keyboard.press("End")
                try:
                    page.wait_for_function(
                        "([selector, before]) => document.querySelectorAll(selector).length > before",
                        arg=[SEARCH_TITLE_SELECTOR, count], timeout=SEARCH_SCROLL_WAIT_MS,
                    )
                    same = 0
                except Exception:
                    same += 1
                    if same >= SEARCH_STOP_REPEATS:
                        break
                count = page.locator(SEARCH_TITLE_SELECTOR).count()
            found, shown = parse_search_print_pages(page.content())
            pages.update(found)
            errors.append(f"[안내] 네이버 검색 화면 기사 {shown}건 확인, "
                          f"그중 지면 {sum(1 for k in found if not k.startswith('제목:'))}건")
        except Exception as error:
            errors.append(f"지면 확인용 네이버 검색 화면을 읽지 못했습니다: {error}")
        browser.close()
    return pages, errors


def find_print_page_for(article, print_pages):
    """검색 화면에서 찾은 지면 중 이 기사의 것. 링크로 먼저, 안 되면 제목으로 맞춘다."""
    for link in (article.get("original_link"), article.get("naver_link")):
        if link and normalize_url(link) in print_pages:
            return print_pages[normalize_url(link)]
    return print_pages.get("제목:" + title_key(article.get("title")))


# =========================================================
# 전체 수집
# =========================================================

def collect(keywords, since, until=None, known_keys=None, known_print=None):
    """
    키워드 목록으로 since ~ until 사이에 발행된 기사를 모은다. (until이 없으면 지금까지)
    known_keys에 있는 기사(이미 DB에 있는 기사)는 페이지 보강을 건너뛰고,
    known_print에 있는 기사(지면이 이미 확인된 기사)만 있으면 지면 확인도 건너뛴다.
    반환: (새 기사 목록, 이미 있던 기사의 (key, keyword) 목록, 오류·안내 목록, {기사 key: 지면})
    """
    known_keys = known_keys or set()
    session = requests.Session()

    found = {}
    keyword_hits = []
    errors = []
    for keyword in keywords:
        try:
            articles = search_keyword(keyword, since, until, session)
        except IncompleteSearch as warning:
            articles = warning.results
            errors.append(f"[{keyword}] {warning}")
        except (NaverApiError, requests.RequestException) as error:
            errors.append(f"[{keyword}] {error}")
            continue
        for article in articles:
            key = article_key(article)
            keyword_hits.append((key, keyword))
            if key not in found:
                found[key] = article

    new_articles = [a for k, a in found.items() if k not in known_keys]
    enrich_articles(new_articles)

    # 검색 결과 화면의 지면 표시로 기간 안 모든 기사(이미 DB에 있던 기사 포함)의 지면을 찾는다
    print_updates = {}
    # 이미 지면이 확인된 기사는 다시 찾을 필요가 없다. 확인할 기사가 없으면 브라우저를 열지 않는다
    known_print = known_print or set()
    if any(key not in known_print for key in found):
        try:
            print_pages, print_errors = search_print_pages(keywords, since, until)
            errors.extend(print_errors)
            for key, article in found.items():
                page = find_print_page_for(article, print_pages)
                if page:
                    print_updates[key] = page
                    if key not in known_keys and article.get("print_page", "온라인") == "온라인":
                        article["print_page"] = page
            shown = sum(1 for k in print_pages if not k.startswith("제목:"))
            errors.append(f"[안내] 지면 표시 {shown}건 중 이번 목록 기사와 맞춘 것 {len(print_updates)}건")
        except Exception as error:
            errors.append(f"지면 확인을 하지 못했습니다: {error}")

    return new_articles, keyword_hits, errors, print_updates
