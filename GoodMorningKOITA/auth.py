"""
비밀번호 로그인

.env에 APP_PASSWORD가 있으면 모든 화면과 API에 로그인이 필요하다.
(다른 PC에서 접속할 수 있게 열 때 아무나 키워드·기사를 지우지 못하도록)
"""

import hashlib
import hmac
import os
import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "goodmorning_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30일

# 서버를 다시 켜면 바뀌므로, 재시작하면 다시 로그인해야 한다
_SESSION_KEY = secrets.token_bytes(32)

LOGIN_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoodMorningKOITA 로그인</title>
<style>
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:#f6f7f9; color:#1d2330;
         font:14px/1.5 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif; }
  form { background:#fff; border:1px solid #e3e6eb; border-radius:10px; padding:24px; width:min(320px, calc(100vw - 32px));
         display:grid; gap:12px; }
  h1 { font-size:18px; margin:0; }
  input, button { font:inherit; border:1px solid #e3e6eb; border-radius:6px; padding:8px 10px; }
  button { background:#2457d6; color:#fff; border-color:#2457d6; cursor:pointer; }
  .error { color:#b91c1c; margin:0; }
</style></head>
<body><form method="post" action="/login">
  <h1>☀️ GoodMorningKOITA</h1>
  {error}
  <input type="password" name="password" placeholder="비밀번호" autofocus required>
  <button type="submit">들어가기</button>
</form></body></html>"""


def password():
    return os.getenv("APP_PASSWORD", "").strip()


def _token():
    return hmac.new(_SESSION_KEY, password().encode(), hashlib.sha256).hexdigest()


def is_logged_in(request: Request):
    if not password():
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE_NAME, ""), _token())


async def require_login(request: Request, call_next):
    if request.url.path == "/login" or is_logged_in(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "로그인이 필요합니다."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def login_page(error=""):
    message = f'<p class="error">{error}</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.replace("{error}", message))


async def login(request: Request):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("password", "")).encode(), password().encode()):
        return login_page("비밀번호가 맞지 않습니다.")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, _token(), max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
        secure=request.headers.get("x-forwarded-proto") == "https",
    )
    return response
