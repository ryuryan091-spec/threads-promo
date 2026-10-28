# p76 — 사건파일 76 운영 자동화

사건파일 76 페이지의 운영 캘린더(휴재) · 휴재 콘텐츠 게시 · 성과 수집 · 매일 점검.
본편 게시는 fb-page-publisher 가 계속 맡는다(이 패키지는 본편을 게시하지 않는다).

threads-promo 에 **함께 두지만 분리해 운영**한다 (DN2026_0069 — 공개 레포 위험 수용).
`p76/` 는 `src/` 를 import 하지 않고, 설정·토큰·알림 대상도 공유하지 않는다(접두어 `FACE_PAGE76_` · `PAGE76_`).

설계: 노션 자비스76 › 상세설계 DSN-P76-OPS-001 · 보안설계 SEC-P76-001 · 요구사항·T1·WBS

## 파이프라인

| 워크플로 | 시각(KST) | 하는 일 | 스위치 |
|---|---|---|---|
| `p76_health.yml` | 매일 09:10 | 페이지 접근·토큰 권한·노션 스키마·휴재 중 본편 승인·예약 작업 누락 점검 | 항상 실행(읽기만) |
| `p76_calendar.yml` | 매일 21:28 | DB-05 실제 발행 기준으로 21일치 DB-09 upsert (완결 다음 날부터 7일 휴재) | `PAGE76_ENABLED` |
| `p76_hiatus_publish.yml` | 매일 20:05 + 0~40분 | DB-09 휴재콘텐츠 `승인`+`검수완료` 1건 → 사진 게시 | `+ PAGE76_HIATUS_PUBLISH_ENABLED` |
| `p76_insights.yml` | 매일 06:20 | 페이지 게시물 목록에서 게시 후 D1·D3·D7 날의 첫 게시물(회차 릴스)의 반응·댓글수·공유수·팔로워 → DB-08 (게시일→회차는 DB-09·기준점으로 매칭) | `+ PAGE76_INSIGHTS_ENABLED` |

| `p76_novel.yml` | 2시간 간격(08:50~22:15) | 사건 소설(B형) 바이블·회차 원고 자동 작성 → 검수 → DB-12, 바이블 묵시 승인(4시간), 승인 회차 게시 | `+ PAGE76_NOVEL_ENABLED` (게시는 `+ PAGE76_NOVEL_PUBLISH_ENABLED`) |

캘린더는 21:45 예약 작업 전에 돌고, CHAT 트리거(21:19)와 6분 이상 떨어지도록 21:28 에 둔다.
CI 는 레포 공용 `ci.yml` 이 `p76` 린트 · 비밀 패턴 검사 · `tests/p76` 를 함께 돌린다.

## Secret 연결

| 코드가 읽는 이름 | 워크플로가 넣는 Secret | 비고 |
|---|---|---|
| `FACE_PAGE76_ID` · `FACE_PAGE76_TOKEN` | 같은 이름 | 사건파일 76 페이지 전용 |
| `PAGE76_NOTION_TOKEN` | `NOTION_TOKEN` | threads-promo 공용 노션 통합(DN2026_0070) — 자비스76·DB-05 에 이 통합을 연결해야 함 |
| `PAGE76_TELEGRAM_BOT_TOKEN` · `PAGE76_TELEGRAM_CHAT_ID` | `TELEGRAM_BOT_TOKEN` · `TELEGRAM_ALERT_CHAT_ID` | threads-promo 운영 알림 방과 공용 — 메시지 앞에 `[사건파일76]` |
| `PAGE76_DB05_ID` · `DB08` · `DB09` · `DB11` | 같은 이름 | 원장 4개(읽기 1 + 쓰기 3) |
| `PAGE76_CLAUDE_KEY` | `CLAUDE_AI_KEY` | threads-promo 기존 Anthropic 키 공용(DN2026_0071) — 소설은 코드 회로(월 예산)로만 상한 |

## 사건 소설 B형 (DSN-P76-NOVEL-001 v0.2 · 002 v0.1)

Claude API(기존 `CLAUDE_AI_KEY`)로 주 1회 연재 소설(7화)의 원고를 쓴다. **게시는 회장이 DB-12 에서 '승인'한 회차만** 한다.

| 단계 | 내용 |
|---|---|
| 바이블 | 시즌 제1화 20일 전, AI 가 소재(창작·소재 메모 PM·게시된 수사일지)를 골라 작성 → 규칙 검사 → 검수(Sonnet) → 통과하면 전용 텔레그램 채널에 알림(09~18시) → **4시간 뒤 첫 점검에서 정본 등록**(D-N7). 검수 플래그가 있으면 자동 등록하지 않음 |
| 회차 | 정본 바이블의 각 화를 게시일 14일 전부터 1화씩(시즌 개시 전 1~3화 버퍼) → 규칙 검사(재작성 1회) → 검수(6·7화·경계값은 Opus 재검수) → '검토대기' |
| 게시 | '승인' + 게시일 10시 이후 + 하루 1건. 게시 직전 회장 편집본을 다시 검사하고, 법률 블록(노션 정본, 확인일 90일 이내)을 치환하고, 고정 꼬리말(픽션·AI 고지)을 붙여 카드 사진으로 게시 |

중단: ① DB-12 행의 `중단` 체크 ② 전용 채널에 `중단` 또는 `/p76_stop` ③ Variable `PAGE76_NOVEL_ENABLED=false`. 재개는 노션에서만.

**기존 Secret 재사용 — 새로 만들지 않는다**

| 코드가 읽는 이름 | 워크플로가 넣는 기존 Secret | 할 일 |
|---|---|---|
| `PAGE76_CLAUDE_KEY` | `CLAUDE_AI_KEY` | 없음(CHAT·답글·쇼츠와 같은 키) |
| `PAGE76_NOTION_TOKEN` | `NOTION_TOKEN` | 노션 연결만 — 아래 표 |
| `PAGE76_DB05_ID` · `PAGE76_DB11_ID` · `FACE_PAGE76_ID` · `FACE_PAGE76_TOKEN` | 같은 이름(p76 운영에서 등록) | 없음 |
| `PAGE76_TELEGRAM_BOT_TOKEN` · `PAGE76_TELEGRAM_CHAT_ID` | `TELEGRAM_BOT_TOKEN` · `TELEGRAM_ALERT_CHAT_ID` | 없음(최우선 장애 알림만) |

**새로 등록하는 Secret (기존에 없는 대상만)**

| 이름 | 값 | 비고 |
|---|---|---|
| `PAGE76_DB12_ID` | 자비스76 하위 「DB-12 소설 원고」 링크의 32자리(**database ID**) | 필수 |
| `PAGE76_NOVEL_PROMPT_PAGE_ID` | 자비스76 하위 「⚙️ 소설 프롬프트 정본」 페이지 링크의 32자리 | 필수 |
| `PAGE76_NOVEL_MEMO_PAGE_ID` | 사건파일 76 하위 「🗂️ 스토리 소재 메모 (비공개)」 페이지 링크의 32자리 | 선택 — 없으면 소재 메모 없이 창작·일지만 사용 |
| `PAGE76_NOVEL_TG_BOT_TOKEN` · `PAGE76_NOVEL_TG_CHAT_ID` | 소설 전용 봇·비공개 채널(관리자: 회장·봇만) | D-N9. getUpdates 로 중단 명령을 읽어 다른 소비자와 봇을 공유하지 않는다 |
| `PAGE76_BANNED_TERMS` | 금칙어(쉼표 구분) | 선택. 로컬 대조만 — API·로그에 넣지 않음 |

ID 는 토큰이 아니지만 공개 레포의 Actions 로그에서 가려지도록 Secret 에 둔다(값은 레포에 적지 않는다).

**노션 연결 (`NOTION_TOKEN` 통합)**: DB-12·프롬프트 정본은 자비스76 하위라 자비스76 연결로 함께 열린다. 소재 메모는 루트 「사건파일 76」 하위라 따로 연결해야 하며, 연결하면 threads-promo 공용 통합이 소재 메모를 읽게 된다(착수 가이드 §3 C3 예외 — 쓰지 않으면 `PAGE76_NOVEL_MEMO_PAGE_ID` 를 등록하지 않는다).

| 이름 | 종류 | 내용 |
|---|---|---|
| `PAGE76_NOVEL_ENABLED` · `PAGE76_NOVEL_PUBLISH_ENABLED` | Variable | 생성 스위치 · 게시 스위치 |
| `PAGE76_NOVEL_START` · `PAGE76_NOVEL_BUDGET_USD` | Variable | 시즌 1 제1화 게시일(기본 2026-11-29) · 월 예산(기본 20) |

의존성: `p76/requirements-novel.txt`(anthropic SDK, 해시 고정 · Linux 러너 전용). 프롬프트 본문은 레포에 없고 노션 정본에서 읽는다(제목에 "초안"이 있으면 실행 중지).

## 캘린더 기준점

DB-05 에 발행 기록(게시상태 발행완료 + 발행일시)이 없는 반수동 운영에서는 Variable 로 운영 계획을 알려 준다.

- `PAGE76_SCHEDULE_ANCHOR=21@2026-10-26` — #021 이 10-26 에 나갔고 이후 매일 연재(완결 뒤 휴재 포함)
- DB-05 의 실제 발행 기록이 이 회차보다 앞서면 DB-05 를 기준으로 한다(지연 반영)

## 모드 (`PAGE76_MODE`)

| 모드 | Graph 쓰기 | 노션 쓰기 | 실행 기록(DB-11) |
|---|---|---|---|
| `dry_run` (기본) | 없음 | 없음 | 없음 |
| `shadow` | 없음 | 캘린더·성과만 | 남김 |
| `live` | 휴재 콘텐츠 게시 | 전부 | 남김 |

오타 등 그 밖의 값은 `dry_run` + 경고. 수동 실행은 Actions 화면의 `mode` 선택이 우선한다.

## 현재 토큰 권한으로 되는 것 / 안 되는 것 (2026-10-28)

토큰 권한: `pages_show_list` · `business_management` · `pages_read_engagement` · `pages_manage_posts` · `public_profile`(앱 기본)

| 기능 | 상태 |
|---|---|
| 캘린더 · 휴재 콘텐츠 게시 · 점검 | ✅ 가능 |
| 성과: 반응·공유·댓글 수·팔로워 | ✅ 가능 (댓글 수가 권한으로 막히면 빈칸 + 수집결과 코드) |
| 성과: 조회·고유 조회·클릭 | ⏸ `read_insights` 필요 → 발급 후 Variable `PAGE76_INSIGHTS_METRICS=post_media_view,post_total_media_view_unique,post_clicks` 와 `PAGE76_EXPECTED_SCOPES` 갱신 |
| 릴스 지표 | ⏸ 엔드포인트 미확정(F-04) — 빈칸 |
| 댓글 보호(숨김·위기 알림) · 시즌 리포트 | ⏸ 미구현 — 댓글 관리 권한 발급 후 별도 PR |

## 보안 (공개 레포)

- **Actions 로그는 누구나 본다.** 로그·알림에는 날짜·회차·상태·코드만 출력하고 본문·토큰·ID 는 내보내지 않는다(마스킹 + `::add-mask::`)
- Secret 을 쓰는 job 은 `if: github.ref == 'refs/heads/main'`. p76 워크플로는 PR 이벤트로 실행되지 않는다(포크 PR 에 Secret 미주입)
- 전 워크플로 `permissions: contents: read`, 공식 액션 커밋 SHA 고정, `requests` 해시 고정(`p76/requirements.txt`)
- DB-05 는 읽기 전용(코드에서 쓰기 호출 차단 + 테스트)
- 회로 차단: Graph code 190 · 10 · 200대 · 368 → 그 실행의 Graph 쓰기 중단, 종료코드 7, 최우선 알림
- 답글·DM·댓글 숨김 엔드포인트 없음
- 공개 레포라 Environment Secret 을 쓸 수 있다 — 레포 소유자가 Environment `live`(main 한정)를 만들면 Secret 을 옮길 것을 권장

## 켜는 순서

1. Actions 에서 `📕 p76-health` 수동 실행 → 로그의 H1~H7 확인
2. `📕 p76-calendar` 수동 실행 `mode=dry_run` → create/update 건수·다음 휴재 기간 확인
3. Variables `PAGE76_ENABLED=true`, `PAGE76_MODE=shadow` → 다음 날 DB-09·DB-11 확인
4. 휴재 1일차 전날(11-09) `PAGE76_MODE=live`, `PAGE76_HIATUS_PUBLISH_ENABLED=true`
5. 성과 수집은 `PAGE76_INSIGHTS_ENABLED=true` (shadow 에서도 DB-08 기록)

긴급 중지: Variable `PAGE76_ENABLED=false` (health 만 계속 실행).

## 개발

```bash
ruff check p76 tests/p76 && python scripts/p76_secret_scan.py p76 tests/p76 && python -m pytest -q tests/p76
```

테스트는 합성 데이터만 쓰고 실제 Graph·노션을 호출하지 않는다.
