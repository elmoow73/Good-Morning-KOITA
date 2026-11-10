#!/usr/bin/env bash
# GoodMorningKOITA 데이터를 구글 드라이브로 백업 (rclone)
#
#   bash GoodMorningKOITA/deploy/backup.sh                 지금 한 번 백업
#   bash GoodMorningKOITA/deploy/backup.sh --install-cron  매일 새벽 3시(한국 시간) 자동 백업 등록
#
# 구글 드라이브 연결(rclone remote 'gdrive')은 처음 한 번 사람이 해야 한다: deploy/README.md 참고
# 선택 환경변수: RCLONE_REMOTE (기본 gdrive), DRIVE_FOLDER (기본 GoodMorningKOITA), KEEP_DAYS (기본 30)
# 출력: 마지막 결과를 stdout에 JSON 한 줄. 종료 코드: 0 성공, 4 드라이브 미연결, 1 그 밖의 오류
#
# 드라이브에 올라가는 것
#   GoodMorningKOITA/최신/koita_news_연도.sqlite·csv·json (연도별 모두), news.db   (늘 최신으로 덮어씀)
#   GoodMorningKOITA/날짜별/YYYY-MM-DD/...                         (KEEP_DAYS일 지나면 지움)
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$DEPLOY_DIR/.deploy.env"
[ -f "$STATE_FILE" ] && . "$STATE_FILE"
KOITA_DATA="${KOITA_DATA:-$DEPLOY_DIR/data}"
REMOTE="${RCLONE_REMOTE:-gdrive}"
FOLDER="${DRIVE_FOLDER:-GoodMorningKOITA}"
KEEP_DAYS="${KEEP_DAYS:-30}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

log() { echo "[backup] $*" >&2; }
result() { printf '{"ok":%s,"step":"%s","message":"%s"%s}\n' "$1" "$2" "$3" "${4:-}"; }

if [ "${1:-}" = "--install-cron" ]; then
  line="0 18 * * * bash $DEPLOY_DIR/backup.sh >> $DEPLOY_DIR/backup.log 2>&1"   # UTC 18시 = 한국 3시
  ( crontab -l 2>/dev/null | grep -vF "$DEPLOY_DIR/backup.sh" || true; echo "$line" ) | crontab -
  result true cron "매일 한국 시간 03:00 자동 백업을 등록했습니다"
  exit 0
fi

if ! command -v rclone >/dev/null 2>&1; then
  log "rclone 설치 중"
  curl -fsSL https://rclone.org/install.sh | $SUDO bash >&2
fi
if ! rclone listremotes 2>/dev/null | grep -qx "${REMOTE}:"; then
  result false remote "구글 드라이브 연결($REMOTE)이 없습니다. deploy/README.md의 '구글 드라이브 연결'을 먼저 하세요."
  exit 4
fi

# SQLite는 쓰는 중에 그냥 복사하면 깨질 수 있어서, sqlite 백업 기능으로 일관된 사본을 만든다
snap="$(mktemp -d)"; trap 'rm -rf "$snap"' EXIT
python3 - "$KOITA_DATA" "$snap" <<'PY'
import pathlib, shutil, sqlite3, sys
src, dst = map(pathlib.Path, sys.argv[1:])
archive = src / "database"
for db in [src / "news.db", *sorted(archive.glob("koita_news*.sqlite"))]:  # 연도별 DB(koita_news_2026 …) 모두
    if db.exists():
        s, d = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(dst / db.name)
        s.backup(d)
        s.close(); d.close()
for f in [*archive.glob("koita_news*.csv"), *archive.glob("koita_news*.json")]:
    shutil.copy2(f, dst / f.name)
PY

today="$(TZ=Asia/Seoul date +%F)"
count="$(ls "$snap" | wc -l)"
rclone copy "$snap" "$REMOTE:$FOLDER/최신" >&2
rclone copy "$snap" "$REMOTE:$FOLDER/날짜별/$today" >&2
rclone delete "$REMOTE:$FOLDER/날짜별" --min-age "${KEEP_DAYS}d" >&2 || true
rclone rmdirs "$REMOTE:$FOLDER/날짜별" --leave-root >&2 || true
result true done "백업 완료" ",\"files\":$count,\"remote\":\"$REMOTE:$FOLDER\",\"date\":\"$today\""
