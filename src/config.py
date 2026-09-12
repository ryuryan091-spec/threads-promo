"""전역 상수 정의.

여기 있는 값은 런타임에 변하지 않는 것만 둔다.
계정 자격증명처럼 변하는 값은 환경변수(env.py)에서 읽는다.
"""

# ---------------------------------------------------------------------------
# 유입 대상 링크 (지시사항: 상수화)
# ---------------------------------------------------------------------------
# TODO(마스터 확인 필요): YouTube 채널 핸들을 실제 값으로 교체할 것.
#   추측 입력 금지. 미교체 상태로 실행하면 main.py가 시작 시점에 중단한다.
YOUTUBE_URL = "https://www.youtube.com/@REPLACE_ME"
X_URL = "https://x.com/tiger18272"

# OAuth 리디렉션 착지 페이지 (docs/index.html -> GitHub Pages)
# Meta 콘솔 Client OAuth Settings 에 이 값과 완전히 동일하게 등록해야 한다.
REDIRECT_URI = "https://ryuryan091-spec.github.io/threads-promo/"

YOUTUBE_URL_PLACEHOLDER = "@REPLACE_ME"

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

MEDIA_TYPE_IMAGE = "IMAGE"
MEDIA_TYPE_TEXT = "TEXT"

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
