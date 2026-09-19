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

VERSION = "1.1.1"   # v1.1.1: CHAT 기업명 차단 규칙 분리(일반어 오차단 수정)

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
# CHAT 도입으로 하루 게시물이 약 10건이 된다. 5개면 반나절치만 보므로
# 최근 REPLY_SCAN_HOURS 시간 안의 글을 최대 REPLY_SCAN_POSTS 개까지 본다.
REPLY_SCAN_POSTS = 20               # 최근 내 글 최대 몇 개까지 훑을지
REPLY_SCAN_HOURS = 24               # 이 시간 안에 발행된 글만 스캔
REPLY_SCAN_LIMIT = 25               # 글당 조회할 댓글 수
# 실행 횟수가 하루 10회 이상으로 늘어나므로 실행당 상한을 따로 둔다(몰아 달기 방지).
REPLY_PER_RUN_CAP = int(os.environ.get("REPLY_PER_RUN_CAP", "4"))
# 한 원글 스레드에서 같은 사람과 주고받는 답글 누적 상한(핑퐁 방지, 기간 무관).
REPLY_THREAD_AUTHOR_CAP = 3

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

# ---------------------------------------------------------------------------
# 인덱스 축
#   콘텐츠 선택을 날짜 하나로만 정하면, 같은 날 두 번 발행할 때
#   이미지·기둥·소재가 전부 같아진다. 실행 구분자를 축으로 하나 더 둔다.
#   시각 기반은 쓰지 않는다. 같은 슬롯 재실행 시 결과가 달라져 멱등성이 깨진다.
# ---------------------------------------------------------------------------
#   곱셈(day * N + disc)은 쓰지 않는다. N 이 로테이션 길이의 배수면
#   나머지가 항상 같아져 한 슬롯이 같은 기둥만 뽑는다(실측 확인).
#   덧셈(day + disc)은 로테이션 길이와 무관하게 항상 안전하다.
DISCRIMINATOR_MAX = 8

# 실행 구분자. 정기 슬롯과 이벤트 발행이 겹치지 않도록 값을 분리한다.
DISCRIMINATOR_BY_SLOT = {"A": 0, "B": 1, "C": 2, "MANUAL": 0}
DISCRIMINATOR_EVENT = 4

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

# 자산 수량 하한. 주 9~10회 발행 시 2주 주기를 확보하려면 20개가 필요하다.
# 미달이면 경고만 남기고 발행은 계속한다.
ASSET_COUNT_RECOMMENDED = 20

# ---------------------------------------------------------------------------
# 인사이트 수집 (읽기 전용)
#   Threads 에 아무것도 쓰지 않는다. 발행과 완전히 독립된 경로다.
# ---------------------------------------------------------------------------
INSIGHTS_ENABLED = os.environ.get(
    "INSIGHTS_ENABLED", "true"
).strip().lower() not in ("false", "0", "no")
INSIGHTS_LOOKBACK_DAYS = int(os.environ.get("INSIGHTS_LOOKBACK_DAYS", "7"))
# CHAT 도입 후 하루 게시물 약 10건. 7일 룩백을 덮으려면 70건이 필요하다.
INSIGHTS_POST_LIMIT = int(os.environ.get("INSIGHTS_POST_LIMIT", "70"))

# 팔로워 100명 미만이면 follower_demographics 를 가져올 수 없다(공식 제약).
INSIGHTS_DEMOGRAPHICS_MIN_FOLLOWERS = 100

# 공식 제약: 이 이전 타임스탬프는 거부된다 (2024-04-13).
INSIGHTS_EARLIEST_TIMESTAMP = 1712991600

# ---------------------------------------------------------------------------
# 기둥 비중 자동 조절
#   표본이 부족한 상태에서 켜면 노이즈를 따라 비중이 진동한다.
#   기본 비활성. 최소 30일 데이터가 쌓인 뒤 수동 판단과 대조하고 켠다.
# ---------------------------------------------------------------------------
ADAPTIVE_WEIGHTS_ENABLED = os.environ.get(
    "ADAPTIVE_WEIGHTS_ENABLED", "false"
).strip().lower() in ("true", "1", "yes")

# 수동 지정. 설정하면 자동 조절보다 우선한다.
PILLAR_ROTATION_OVERRIDE = os.environ.get("PILLAR_ROTATION_OVERRIDE", "").strip()
LAST_WEIGHT_ADJUST = os.environ.get("LAST_WEIGHT_ADJUST", "").strip()

# 안전장치 7종
WEIGHT_MIN_SAMPLE = 10          # S1 기둥당 최소 발행 건수
WEIGHT_ADJUST_INTERVAL_DAYS = 30  # S2 조정 주기
WEIGHT_ADJUST_STEP = 1          # S3 1회 조정 폭(칸)
WEIGHT_MIN_SLOTS = 1            # S4 기둥 하한
WEIGHT_PROMO_MAX_SLOTS = 2      # S5 PROMO 상한
WEIGHT_SIGNIFICANCE_RATIO = 1.5  # S6 1위/2위 비율 임계
WEIGHT_STORY_MIN_SLOTS = 2      # S7 근거 보유 기둥 하한

# 점수 가중. 조회·좋아요는 행동으로 이어지지 않아 제외한다.
WEIGHT_SCORE_CLICKS = 1.0
WEIGHT_SCORE_REPLIES = 0.3

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

# 마지막 갱신일. 주 1회 갱신 워크플로우가 Secret 에 기록한다.
# 만료 판정은 발급일과 갱신일 중 더 늦은 쪽을 기준으로 한다.
TOKEN_REFRESHED_AT = os.environ.get("TOKEN_REFRESHED_AT", "").strip()

# 발행·답글 실행에서 토큰을 갱신할지 여부.
#   기본 false. 매 실행 갱신은 60일 토큰을 하루 수십 번 갱신하는 셈이라
#   Meta 문서의 갱신 조건(발급 후 24시간 경과)에도 어긋나고,
#   자동 보안 시스템에 이상 패턴으로 보인다.
#   갱신은 token_refresh.yml 이 주 1회만 수행한다.
REFRESH_ON_EVERY_RUN = os.environ.get(
    "REFRESH_ON_EVERY_RUN", "false"
).strip().lower() in ("true", "1", "yes")

SECRET_REFRESHED_AT_NAME = "TOKEN_REFRESHED_AT"
TOKEN_LIFETIME_DAYS = 60

# 갱신 임계. 잔여가 이 값 이하일 때만 갱신한다.
#   판정은 날짜 계산이라 API 호출이 0회다. 매일 확인해도 비용이 없다.
#   실제 갱신은 약 50일에 1회. 임계 이후 10회의 재시도 기회가 확보된다.
TOKEN_REFRESH_THRESHOLD_DAYS = int(
    os.environ.get("TOKEN_REFRESH_THRESHOLD_DAYS", "10")
)

# 경보 임계는 갱신 임계에 맞춘다.
#   "갱신이 돌았어야 하는데 아직 안 됐다"는 신호여야 의미가 있다.
#   갱신 임계보다 넓게 잡으면 정상 상태에서 매일 경보가 울린다.
TOKEN_WARN_DAYS = TOKEN_REFRESH_THRESHOLD_DAYS   # 갱신 시도 구간 진입
TOKEN_URGENT_DAYS = 5                            # 5회 실패
TOKEN_CRITICAL_DAYS = 2                          # 재인가 임박

# ---------------------------------------------------------------------------
# Notion Tracker DB (STORY 기둥 근거)
#   스키마를 모르는 상태에서 컬럼명을 추측하지 않는다.
#   타입 기준으로 안전한 것(title/select/status/multi_select)만 기본 채택하고,
#   그 외 컬럼은 NOTION_FIELD_ALLOWLIST 에 명시할 때만 쓴다.
# ---------------------------------------------------------------------------
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "").strip()
NOTION_EPISODE_LIMIT = int(os.environ.get("NOTION_EPISODE_LIMIT", "6"))
NOTION_FIELD_ALLOWLIST = os.environ.get("NOTION_FIELD_ALLOWLIST", "").strip()

# 트래커 상태 필터. 진행중 회차를 Threads 가 먼저 언급하는 사고를 막는다.
# 속성명을 추측하지 않는다. 비워두면 필터를 적용하지 않는다.
NOTION_STATUS_PROPERTY = os.environ.get("NOTION_STATUS_PROPERTY", "발행 상태").strip()
NOTION_STATUS_VALUE = os.environ.get("NOTION_STATUS_VALUE", "완료").strip()

# ---------------------------------------------------------------------------
# 이벤트 기반 STORY 발행
#   EDT 회차가 올라오면 그 회차를 근거로 STORY 를 발행한다.
#   신규 판정은 created_time 시간창으로 한다(무상태).
# ---------------------------------------------------------------------------
EVENT_STORY_ENABLED = os.environ.get(
    "EVENT_STORY_ENABLED", "false"
).strip().lower() in ("true", "1", "yes")
EVENT_WINDOW_HOURS = float(os.environ.get("EVENT_WINDOW_HOURS", "7.2"))
EVENT_DAILY_CAP = int(os.environ.get("EVENT_DAILY_CAP", "2"))
EVENT_MIN_GAP_HOURS = float(os.environ.get("EVENT_MIN_GAP_HOURS", "4"))
ANTIBOT_EVENT_JITTER = (600, 3000)   # 10~50분


# ---------------------------------------------------------------------------
# CHAT (오전 시장 잡담)
#   KST 09:00~12:05 창에서 텍스트 잡담을 하루 CHAT_DAILY_MIN~MAX 건 발행한다.
#   본문에 표식을 넣지 않는다. "창 안에서 발행된 글 = CHAT" 으로 정의한다.
#   정기 슬롯(08:23/12:47)·이벤트 슬롯(03:11/13:29/17:41/23:17)과 겹치지 않는다.
#   기본 비활성. Variables CHAT_ENABLED=true 로 켠다.
# ---------------------------------------------------------------------------
CHAT_ENABLED = os.environ.get(
    "CHAT_ENABLED", "false"
).strip().lower() in ("true", "1", "yes")
CHAT_WINDOW_START = "09:00"   # KST
CHAT_WINDOW_END = "12:05"     # KST. 이 시각 이후 시작한 실행은 즉시 종료

# chat.yml cron 과 1:1 대응. 순서가 곧 트리거 번호(T1~T9).
CHAT_TRIGGERS: tuple[str, ...] = (
    "09:04", "09:23", "09:44", "10:07", "10:26",
    "10:48", "11:09", "11:31", "11:52",
)
CHAT_DAILY_MIN = int(os.environ.get("CHAT_DAILY_MIN", "6"))
CHAT_DAILY_MAX = int(os.environ.get("CHAT_DAILY_MAX", "8"))
CHAT_MIN_GAP_MIN = 15          # 직전 게시물(종류 무관)과 최소 간격(분)
# 간격이 모자라면 버리지 않고 이 한도 안에서 기다렸다 발행한다.
# 트리거 간격(19~23분)이 cron 지연·지터와 겹치면 간격 미달이 자주 난다.
# 버리면 일일 목표를 못 채운다(전수 테스트 시뮬레이션에서 확인, 2026-09-19).
CHAT_MAX_GAP_WAIT_SEC = 600
CHAT_JITTER = (30, 240)        # 발행 전 랜덤 지연(초)
CHAT_TEXT_MAX_LEN = 200
CHAT_RECENT_FOR_DEDUP = 12
CHAT_SALT = "chat"

# 근거 소스: mix(RSS·웹검색 랜덤) | rss | web | none
CHAT_SOURCE_MODE = os.environ.get("CHAT_SOURCE_MODE", "mix").strip().lower()

# 본문에 쓸 수 있는 기관·거시 이벤트 일반명사 (마스터 결정 2026-09-19: 기관명 허용)
# 기업명·인물명은 허용하지 않는다(REG-04). 숫자는 일체 금지(REG-03).
CHAT_THEME_ALLOWLIST: tuple[str, ...] = (
    "연준", "Fed", "FOMC", "한국은행", "한은", "금통위", "ECB", "일본은행", "BOJ",
    "재무부", "백악관", "의회", "OPEC",
    "금리", "물가", "인플레이션", "CPI", "PCE", "고용", "실업", "GDP", "경기",
    "유가", "원유", "환율", "달러", "엔화", "위안화", "국채", "채권", "금값", "비트코인",
    "관세", "무역", "실적", "실적 시즌", "반도체", "빅테크", "AI", "에너지", "은행",
    "부동산", "소비", "제조업", "변동성", "위험자산", "안전자산",
)
# 방향 예측 표현. 시장 전망 금지(REG-03).
CHAT_FORECAST_TERMS: tuple[str, ...] = (
    "오를 것", "오를 겁", "오르겠", "떨어질 것", "떨어질 겁", "떨어지겠",
    "빠질 것", "빠지겠", "반등할", "반등하겠", "폭락할", "폭등할",
    "상승할 것", "하락할 것", "갈 것 같", "간다고 봅", "바닥", "고점",
)
# 기업명·인물명 차단 목록(휴리스틱). 영문 고유명은 lint_chat 이 허용목록 대조로 따로 잡는다.
#   v1.1.1: 일반어와 겹치는 항목을 분리했다(Actions dry_run 검토에서 '전자(前者)' 오차단 확인).
#     - 단독 차단: 일반어와 겹치지 않는 고유명
#     - 접미 차단: 앞 글자에 붙어 있을 때만 차단 (삼성전자 O / "전자 쪽" X)
#     - 단어 차단: 뒤에 한글이 2자 이상 붙으면 다른 단어로 본다 (메타가 O / 메타버스 X)
#     - '알파벳'은 일반명사라 제외(기업은 '구글'로 잡는다)
CHAT_ENTITY_TERMS: tuple[str, ...] = (
    "삼성", "하이닉스", "엔비디아", "테슬라", "마이크로소프트", "구글",
    "아마존", "넷플릭스", "브로드컴", "TSMC", "인텔", "현대차", "카카오", "네이버",
    "파월", "트럼프", "바이든", "옐런", "베선트", "머스크", "버핏", "이창용",
)
CHAT_ENTITY_SUFFIXES: tuple[str, ...] = ("전자", "그룹", "홀딩스", "증권", "자산운용")
CHAT_ENTITY_WORDS: tuple[str, ...] = ("메타", "애플")

# 뉴스 RSS. URL 은 Variables 로 주입한다(제공처 교체에 코드 수정 불필요).
# 여러 개는 | 로 구분. 비어 있으면 RSS 소스는 '실패'로 처리되어 폴백한다.
MOOD_RSS_URLS: tuple[str, ...] = tuple(
    u.strip() for u in os.environ.get("MOOD_RSS_URLS", "").split("|") if u.strip()
)
MOOD_RSS_MAX_ITEMS = 10
MOOD_RSS_MAX_AGE_HOURS = 24

# Claude 웹 검색(서버 도구). 마스터 결정 2026-09-19: Supabase 대신 CLAUDE_AI_KEY 로 조회.
#   web_search_20250305 를 쓴다. 20260209 이후 버전은 allowed_callers 기본값이
#   code_execution 이라 별도 도구 구성이 필요하다(공식 문서 확인).
#   과금: 검색 1,000회당 $10 + 토큰. 호출당 검색 횟수를 max_uses 로 제한한다.
#   조직 관리자가 Console 에서 웹 검색을 켜야 한다. 꺼져 있으면 400.
MOOD_WEB_TOOL_TYPE = "web_search_20250305"
MOOD_WEB_MAX_USES = int(os.environ.get("MOOD_WEB_MAX_USES", "2"))

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
