"""전역 상수 정의.

여기 있는 값은 런타임에 변하지 않는 것만 둔다.
계정 자격증명처럼 변하는 값은 환경변수(env.py)에서 읽는다.
"""

# ---------------------------------------------------------------------------
# 유입 대상 링크 (지시사항: 상수화)
#
# 값은 GitHub Variables 로 주입한다. 코드를 편집하지 않으므로 문법 오류가
# 발생할 수 없다. 등록 위치:
#   레포 > Settings > Secrets and variables > Actions > Variables 탭
#     YOUTUBE_URL, X_URL   (비밀값이 아니므로 Secrets 가 아니라 Variables)
# ---------------------------------------------------------------------------
import os

YOUTUBE_URL = os.environ.get("YOUTUBE_URL", "").strip()
X_URL = os.environ.get("X_URL", "").strip()

# OAuth 리디렉션 착지 페이지 (docs/index.html -> GitHub Pages)
REDIRECT_URI = "https://ryuryan091-spec.github.io/threads-promo/"

# ---------------------------------------------------------------------------
# Claude (본문 생성)
#   API 키는 Secret CLAUDE_AI_KEY 로 주입한다.
#   생성 실패 시 정적 텍스트 풀로 자동 폴백하므로 발행이 멈추지 않는다.
# ---------------------------------------------------------------------------
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5").strip()
AI_ENABLED = os.environ.get("AI_ENABLED", "true").strip().lower() not in ("false", "0", "no")
AI_MAX_RETRY = 2          # 린트 실패 시 재생성 횟수
RECENT_POSTS_FOR_DEDUP = 8  # 중복 회피용으로 프롬프트에 넣을 최근 글 수

# ---------------------------------------------------------------------------
# Threads API
# ---------------------------------------------------------------------------
THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_AUTH_BASE = "https://graph.threads.net"

# 공식 문서 기준 제한값
TEXT_MAX_LEN = 500
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MIN_WIDTH = 320
IMAGE_MAX_WIDTH = 1440
IMAGE_MAX_ASPECT_RATIO = 10.0
DAILY_POST_QUOTA = 250
DAILY_REPLY_QUOTA = 1000

# ---------------------------------------------------------------------------
# 답글 엔진 (Reply Engine)
#   X Reply Engine 운영 결정사항 이식: 내 글 댓글만, 좋아요 미사용,
#   외국어는 무응답이 아니라 한국어 정형 문구, 선택형은 중립 감사만.
# ---------------------------------------------------------------------------
REPLY_ENABLED = os.environ.get("REPLY_ENABLED", "true").strip().lower() not in ("false", "0", "no")
REPLY_MAX_LEN = 200                 # 답글 본문 상한
REPLY_DAILY_CAP = 20                # 자체 일일 답글 상한 (API 한도 1000과 별개)
REPLY_AUTHOR_DAILY_CAP = 2          # 같은 사람에게 하루 최대 답글 수
REPLY_SCAN_POSTS = 5                # 최근 내 글 몇 개까지 훑을지
REPLY_SCAN_LIMIT = 25               # 글당 조회할 댓글 수

# ---------------------------------------------------------------------------
# 안티봇
#   동일 일정·동일 문구 금지. 고정 sleep 금지. 일일 상한 필수.
#   슬롯 방식: 여러 cron 중 하루 하나만 실제 실행 -> 시각 분산 + Actions 분 절약
# ---------------------------------------------------------------------------
ANTIBOT_PUBLISH_JITTER = (60, 480)   # 발행 전 1~8분
ANTIBOT_REPLY_JITTER = (20, 150)     # 답글 사이 20초~2.5분
# 주간 휴식일. 0 = 매일 발행, 1 = 주 1회 쉬는 날(요일은 주마다 랜덤).
# 매일 100% 빠짐없이 발행하는 것 자체가 기계적 패턴이라는 판단에 따른 옵션.
# 주 3~4회가 지속 가능 하한이므로 주 6회는 여전히 안전 구간.
PUBLISH_WEEKLY_REST_DAYS = int(os.environ.get("PUBLISH_WEEKLY_REST_DAYS", "0"))

ANTIBOT_SLOT_SALT_PUBLISH = "publish"
ANTIBOT_SLOT_SALT_REPLY = "reply"

MEDIA_TYPE_IMAGE = "IMAGE"
MEDIA_TYPE_TEXT = "TEXT"

# ---------------------------------------------------------------------------
# 발행 폴백 (이중화)
#   이미지 하나 때문에 그날 발행을 거르면, 매일 발행하는 계정이라는 신호를
#   잃는다. 이미지 없이 올리는 편이 낫다는 판단.
# ---------------------------------------------------------------------------
IMAGE_FALLBACK_TO_TEXT = os.environ.get(
    "IMAGE_FALLBACK_TO_TEXT", "true"
).strip().lower() not in ("false", "0", "no")
IMAGE_CANDIDATE_LIMIT = 3   # Tier 2 에서 시도할 대체 이미지 최대 개수

# ---------------------------------------------------------------------------
# 컨테이너 처리 대기
#   Meta 는 컨테이너 생성 후 발행까지 평균 30초 대기를 권장한다.
#   즉시 발행하면 code=24 (Media Not Found) 가 발생한다.
#   상태 조회는 1분 간격, 최대 5분까지 권장.
# ---------------------------------------------------------------------------
CONTAINER_WAIT_IMAGE_SEC = 30     # 이미지 컨테이너 최초 대기
CONTAINER_WAIT_TEXT_SEC = 5       # 텍스트 컨테이너 최초 대기
CONTAINER_POLL_INTERVAL_SEC = 60  # 상태 재조회 간격 (Meta 권장)
CONTAINER_POLL_MAX_SEC = 300      # 총 대기 상한 (Meta 권장)

CONTAINER_STATUS_FINISHED = "FINISHED"
CONTAINER_STATUS_IN_PROGRESS = "IN_PROGRESS"
CONTAINER_STATUS_ERROR = "ERROR"
CONTAINER_STATUS_EXPIRED = "EXPIRED"
CONTAINER_STATUS_PUBLISHED = "PUBLISHED"

# ---------------------------------------------------------------------------
# 토큰 만료 사전 경보
#   갱신 응답의 expires_in 은 '새 토큰'의 수명이다. 저장에 실패하면 그 토큰은
#   버려지므로, 실제로 쓰는 기존 토큰의 남은 수명은 알 수 없다.
#   따라서 저장 실패 시에는 발급일 기준 경과일로 판정한다.
#   TOKEN_ISSUED_AT Variable 에 최초 발급일(YYYY-MM-DD)을 넣으면 경보가 동작한다.
# ---------------------------------------------------------------------------
TOKEN_ISSUED_AT = os.environ.get("TOKEN_ISSUED_AT", "").strip()
TOKEN_LIFETIME_DAYS = 60
TOKEN_WARN_DAYS = 20      # 이하면 경고
TOKEN_URGENT_DAYS = 10    # 이하면 긴급
TOKEN_CRITICAL_DAYS = 3   # 이하면 최우선

# ---------------------------------------------------------------------------
# Notion Tracker DB (STORY 기둥 근거)
#   스키마를 모르는 상태에서 컬럼명을 추측하지 않는다.
#   타입 기준으로 안전한 것(title/select/status/multi_select)만 기본 채택하고,
#   그 외 컬럼은 NOTION_FIELD_ALLOWLIST 에 명시할 때만 쓴다.
# ---------------------------------------------------------------------------
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "").strip()
NOTION_EPISODE_LIMIT = int(os.environ.get("NOTION_EPISODE_LIMIT", "6"))
NOTION_FIELD_ALLOWLIST = os.environ.get("NOTION_FIELD_ALLOWLIST", "").strip()


HTTP_TIMEOUT_SEC = 20
HTTP_RETRY_COUNT = 3
HTTP_RETRY_BACKOFF_SEC = 3

# ---------------------------------------------------------------------------
# 발행 정책
# ---------------------------------------------------------------------------
# 링크는 본문이 아니라 셀프 리플라이에 배치한다.
LINK_PLACEMENT_SELF_REPLY = True

# 홍보형 1 : 관찰형 3 비율. day_of_year % PROMO_CYCLE == 0 이면 홍보형.
PROMO_CYCLE = 4

# ---------------------------------------------------------------------------
# 금칙어 — 투자조언/유사투자자문 및 인게이지먼트 베이트 회피
# ---------------------------------------------------------------------------
# 자본시장법 제101조: 대가를 받고 투자판단에 관한 조언을 업으로 하면 신고 의무.
# 무료 발행이라 현 단계에서 해당은 아니나, 향후 유료화 시 과거 게시물이
# 함께 판단 대상이 되므로 처음부터 종목/목표가/매매권유 표현을 배제한다.
FORBIDDEN_ADVICE_TERMS = (
    "매수", "매도", "목표가", "추천주", "종목추천", "손절가",
    "익절", "풀매수", "몰빵", "수익보장", "확정수익", "리딩",
)

# Threads가 다운랭크하는 저품질 참여 유도 표현.
FORBIDDEN_BAIT_TERMS = (
    "댓글 남기면", "좋아요 누르면", "동의하면 댓글",
    "1번 2번 골라", "팔로우하면", "선착순",
)
