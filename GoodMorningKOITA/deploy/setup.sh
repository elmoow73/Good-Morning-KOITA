#!/usr/bin/env bash
# GoodMorningKOITA 서버 설치·업데이트 (Ubuntu, Oracle Cloud 무료 VM 기준)
#
# 처음 설치:
#   NAVER_CLIENT_ID=... NAVER_CLIENT_SECRET=... APP_PASSWORD=... bash GoodMorningKOITA/deploy/setup.sh
# 업데이트(최신 코드 받아서 다시 빌드): bash GoodMorningKOITA/deploy/setup.sh
#
# 대화형 입력 없이 동작하고, 몇 번을 다시 실행해도 안전하다(멱등).
# 선택 환경변수:
#   DOMAIN      접속 주소. 없으면 공인 IP로 <IP>.sslip.io 를 만든다 (HTTPS 인증서 자동 발급)
#   KOITA_DATA  데이터 폴더 (기본: deploy/data)
#   SKIP_PULL=1 git pull 건너뛰기
# 출력: 진행 로그는 stderr, 마지막 결과는 stdout에 JSON 한 줄
# 종료 코드: 0 성공, 2 설정 누락(.env), 3 서버가 정상 응답하지 않음, 1 그 밖의 오류
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(dirname "$DEPLOY_DIR")"
ENV_FILE="$APP_DIR/.env"
STATE_FILE="$DEPLOY_DIR/.deploy.env"   # DOMAIN 등 서버별 값을 기억 (git에 올리지 않음)

log() { echo "[setup] $*" >&2; }
result() { printf '{"ok":%s,"step":"%s","message":"%s"%s}\n' "$1" "$2" "$3" "${4:-}"; }
fail() { result false "$2" "$3"; exit "$1"; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# 1. 최신 코드
if [ "${SKIP_PULL:-}" != "1" ] && git -C "$APP_DIR" rev-parse >/dev/null 2>&1; then
  log "최신 코드 받는 중"
  git -C "$APP_DIR" pull --ff-only >&2 || log "git pull 실패 (현재 코드로 계속)"
fi

# 2. Docker
if ! command -v docker >/dev/null 2>&1; then
  log "Docker 설치 중"
  curl -fsSL https://get.docker.com | $SUDO sh >&2
  $SUDO usermod -aG docker "$(id -un)" || true
fi
DOCKER="docker"; docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"

# 3. 방화벽: Oracle Ubuntu 이미지는 iptables가 80/443을 막고 있다
if command -v iptables >/dev/null 2>&1; then
  for port in 80 443; do
    if ! $SUDO iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
      log "방화벽 $port 포트 열기"
      $SUDO iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
    fi
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then $SUDO netfilter-persistent save >/dev/null 2>&1 || true; fi
fi

# 4. .env (네이버 키, 로그인 비밀번호, 로그인 유지 비밀값)
[ -f "$ENV_FILE" ] || cp "$APP_DIR/.env.example" "$ENV_FILE"
has_value() { grep -qE "^${1}=.+" "$ENV_FILE" && ! grep -qE "^${1}=여기에" "$ENV_FILE"; }
set_env() {  # 값을 주면 그 값으로 바꾸고(다시 실행해 비밀번호 등을 바꿀 수 있음), 안 주면 그대로 둔다
  local key="$1" value="$2"
  [ -z "$value" ] && return 0
  if grep -qE "^${key}=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import sys, pathlib
path, key, value = sys.argv[1:]
p = pathlib.Path(path)
lines = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in p.read_text(encoding="utf-8").splitlines()]
p.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}
set_env NAVER_CLIENT_ID "${NAVER_CLIENT_ID:-}"
set_env NAVER_CLIENT_SECRET "${NAVER_CLIENT_SECRET:-}"
set_env APP_PASSWORD "${APP_PASSWORD:-}"
has_value SESSION_SECRET || set_env SESSION_SECRET "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
chmod 600 "$ENV_FILE"
for key in NAVER_CLIENT_ID NAVER_CLIENT_SECRET APP_PASSWORD; do
  if ! has_value "$key"; then
    fail 2 env "$ENV_FILE 에 ${key} 값이 없습니다. ${key}=값 을 앞에 붙여 다시 실행하세요."
  fi
done

# 5. 접속 주소
[ -f "$STATE_FILE" ] && . "$STATE_FILE"
if [ -z "${DOMAIN:-}" ]; then
  ip="$(curl -fsS --max-time 10 https://api.ipify.org || true)"
  [ -n "$ip" ] || fail 1 domain "공인 IP를 알아내지 못했습니다. DOMAIN=주소 를 지정하세요."
  DOMAIN="${ip//./-}.sslip.io"
fi
KOITA_DATA="${KOITA_DATA:-$DEPLOY_DIR/data}"
printf 'DOMAIN=%s\nKOITA_DATA=%s\n' "$DOMAIN" "$KOITA_DATA" > "$STATE_FILE"
mkdir -p "$KOITA_DATA"
export DOMAIN KOITA_DATA

# 6. 빌드·실행
log "빌드·실행 중 (처음에는 5~10분 걸립니다): https://$DOMAIN"
$DOCKER compose -f "$DEPLOY_DIR/docker-compose.yml" up -d --build >&2

# 7. 상태 확인
for _ in $(seq 1 60); do
  if out="$($DOCKER compose -f "$DEPLOY_DIR/docker-compose.yml" exec -T app \
        python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())" 2>/dev/null)"; then
    version="$(echo "$out" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version",""))' 2>/dev/null || true)"
    result true done "설치 완료" ",\"url\":\"https://$DOMAIN\",\"version\":\"$version\",\"data_dir\":\"$KOITA_DATA\""
    exit 0
  fi
  sleep 5
done
fail 3 healthz "서버가 응답하지 않습니다. 'docker compose -f $DEPLOY_DIR/docker-compose.yml logs app'으로 확인하세요."
