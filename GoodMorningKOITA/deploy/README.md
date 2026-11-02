# Oracle Cloud 무료 서버에 올리기

PC가 꺼져 있어도 폰·PC에서 GoodMorningKOITA를 쓰도록 Oracle Cloud 평생 무료 서버(서울)에 올리는 방법입니다.
사람이 해야 하는 일은 **1~3단계(가입, 서버 만들기, 포트 열기)**와 **5단계(구글 드라이브 연결) 한 번**뿐이고, 나머지는 스크립트가 합니다.

## 1. Oracle Cloud 가입

1. https://www.oracle.com/kr/cloud/free/ 에서 "무료로 시작하기".
2. **홈 리전(Home Region)을 "South Korea Central (Seoul)"로** 고릅니다. 나중에 바꿀 수 없습니다.
3. 카드 등록은 본인 확인용입니다. "Always Free" 자원만 쓰면 요금이 나가지 않습니다.

## 2. 서버(VM) 만들기

콘솔 → 메뉴 → Compute → Instances → **Create instance**

| 항목 | 고를 것 |
|---|---|
| Image | **Canonical Ubuntu 24.04** |
| Shape | **Ampere (VM.Standard.A1.Flex)**, OCPU 2, 메모리 12GB (무료 범위: 합계 4 OCPU/24GB) |
| Networking | 새 VCN 만들기, **공인 IPv4 주소 할당** 체크 |
| SSH keys | "Generate a key pair" → **개인 키 저장**(꼭 보관) |

"Out of capacity"가 뜨면 몇 시간 뒤 다시 시도하거나, Shape를 `VM.Standard.E2.1.Micro`(무료, 메모리 1GB)로 고릅니다. Micro에서는 지면 확인(브라우저)이 느릴 수 있습니다.

만든 뒤 인스턴스 화면에서 **Public IP address**를 적어 둡니다.

## 3. 80·443 포트 열기 (Oracle 콘솔)

인스턴스 화면 → Subnet 링크 → Security Lists → Default Security List → **Add Ingress Rules**

- Source CIDR `0.0.0.0/0`, IP Protocol TCP, Destination Port Range `80,443`

(서버 안쪽 방화벽은 설치 스크립트가 엽니다.)

## 4. 설치 (명령 세 줄)

PC 터미널(Windows는 PowerShell)에서 서버에 접속합니다.

```
ssh -i 저장한_개인키.key ubuntu@<공인IP>
```

> 브라우저만 있다면 Oracle 콘솔 오른쪽 위의 **Cloud Shell**에서 같은 `ssh` 명령을 써도 됩니다(키 파일 업로드 필요).

서버에서:

```
git clone https://github.com/elmoow73/Good-Morning-KOITA.git
cd Good-Morning-KOITA
NAVER_CLIENT_ID=네이버_ID NAVER_CLIENT_SECRET=네이버_SECRET APP_PASSWORD=로그인_비밀번호 bash GoodMorningKOITA/deploy/setup.sh
```

처음에는 5~10분 걸립니다. 끝나면 마지막 줄에 이렇게 나옵니다.

```
{"ok":true,"step":"done","message":"설치 완료","url":"https://152-70-1-2.sslip.io","version":"1.3.0","data_dir":"..."}
```

그 `url`을 폰 크롬과 PC 크롬·엣지로 열고, 로그인한 뒤 **앱 설치**(폰: 메뉴 → 홈 화면에 추가)를 누르면 끝입니다.
주소는 공인 IP로 자동으로 만들어지고(`sslip.io`), HTTPS 인증서도 자동으로 붙습니다. 내 도메인을 쓰려면 `DOMAIN=koita.example.com`을 앞에 붙여 다시 실행합니다.

### 결과 JSON과 종료 코드

| 종료 코드 | `step` | 뜻 |
|---|---|---|
| 0 | `done` | 설치·업데이트 완료 |
| 2 | `env` | `.env`에 네이버 키나 `APP_PASSWORD`가 없음 |
| 3 | `healthz` | 서버가 응답하지 않음 (`docker compose -f GoodMorningKOITA/deploy/docker-compose.yml logs app`) |
| 1 | 그 밖 | 그 밖의 오류 |

## 업데이트

GitHub에 새 버전이 올라가면 서버에서 같은 스크립트를 다시 실행합니다. 키와 비밀번호는 이미 저장돼 있어서 다시 넣지 않아도 됩니다. 비밀번호를 바꾸려면 `APP_PASSWORD=새_비밀번호`를 앞에 붙여 실행합니다.

```
cd ~/Good-Morning-KOITA && bash GoodMorningKOITA/deploy/setup.sh
```

데이터(`GoodMorningKOITA/deploy/data`)는 업데이트해도 그대로 남습니다.

## 5. 구글 드라이브 자동 백업 (선택)

서버의 기사 DB(SQLite 원본, CSV, JSON)와 검색 결과를 구글 드라이브 `GoodMorningKOITA` 폴더로 매일 백업합니다.
SQLite 파일을 드라이브에서 직접 열어 쓰면 깨질 수 있어서, 원본은 서버에 두고 드라이브에는 **사본**만 올립니다.

1. **PC에서** rclone을 받습니다: https://rclone.org/downloads/ (Windows zip을 풀면 `rclone.exe`).
2. PC 터미널에서 `rclone authorize "drive"` → 브라우저가 열리면 구글 계정으로 허용 → 터미널에 나온 `{...}` 토큰을 복사합니다.
3. **서버에서** (rclone이 없으면 먼저 `curl -fsSL https://rclone.org/install.sh | sudo bash`):
   ```
   rclone config create gdrive drive token='복사한_토큰'
   bash ~/Good-Morning-KOITA/GoodMorningKOITA/deploy/backup.sh               # 지금 한 번
   bash ~/Good-Morning-KOITA/GoodMorningKOITA/deploy/backup.sh --install-cron  # 매일 새벽 3시 자동
   ```

드라이브에는 `GoodMorningKOITA/최신/`(늘 최신)과 `GoodMorningKOITA/날짜별/YYYY-MM-DD/`(30일 보관)가 생깁니다.
`backup.sh`도 마지막 줄에 결과 JSON을 내고, 드라이브 연결이 없으면 종료 코드 4로 끝납니다.

## 코드 수정은 어디서?

코드는 GitHub에 있으니, PC가 꺼져 있어도 폰에서 Claude(claude.ai/code)로 고치고 PR을 합친 뒤 서버에서 위 업데이트 명령만 실행하면 됩니다.
