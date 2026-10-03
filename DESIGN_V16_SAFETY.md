# DESIGN v1.6.0 — 계정 보호(안전) 모드

| 항목 | 내용 |
|---|---|
| 버전 | config 1.6.0 · safety 1.0.0 (신규) |
| 단일 정책 모듈 | `src/safety.py` |
| 적용 경로 | 정기 `main` · CHAT `run_chat` · STORY `run_story` · 댓글 답글/이어쓰기 `run_reply` · 워치독 · golive_check · verify_repo |
| 테스트 | `tests/test_safety_v16.py` (161건), 기존 테스트는 `tests/conftest.py` legacy 프로필로 v1.5.0 동작을 유지 |

---

## 1. 배경 — 사실과 추정을 나눈다

### 1-1. 사실 (확인됨)

| # | 사실 | 근거 |
|---|---|---|
| F1 | 운영 Threads 계정이 '봇 의심'으로 비활성화되었다 | 계정 소유자 확인 |
| F2 | 과거에도 잠금이 반복되었다 (H6) | 계정 소유자 확인 |
| F3 | 정기·이벤트 글마다 외부 링크 2개(YouTube·X)를 담은 셀프 리플라이를 달았다 (H1) | v1.5.0 `src/main.py:360`, `src/run_story.py:346` |
| F4 | 계정 개설 첫날부터 전 경로가 자동화되어 있었다 (H2) | 워크플로 구성 |
| F5 | AI 자동 답글 하루 최대 40건, 외국어 댓글에는 정형 문구 7종을 여러 사용자에게 반복했다 (H3) | v1.5.0 `config.REPLY_DAILY_CAP=40`, `reply_engine.NON_KOREAN_REPLIES` |
| F6 | 텍스트 CHAT 을 평일 5~15건, KST 09:00~24:00 에 발행했다 (H4) | v1.5.0 `CHAT_DAILY_MIN/MAX`, `chat.yml` |
| F7 | 공식 API 한도(24시간 게시 250 · 답글 1,000)에는 근접한 적이 없다 | `threads_publishing_limit` 조회 설계, 워치독 쿼터 검사 |

쓰기 호출 위치(v1.5.0): `main.py` 342/353/360, `run_chat.py` 286, `run_reply.py` 371(댓글 답글)·499(이어쓰기), `run_story.py` 330/339/346.

### 1-2. 추정 (확인 불가)

- Meta 는 개별 계정 집행(비활성화·잠금)의 구체 사유를 공개하지 않는다. 따라서 위 H1~H4 중 무엇이 결정적이었는지는 **확인할 수 없다**.
  (출처: Meta Transparency Center 의 정책 설명은 범주 수준이며 계정별 판정 근거를 제공하지 않는다 — 일반적 관찰, 2차 확인 권장)
- F7 에 따라 한도 초과가 아니라 **행동 패턴**(링크 반복·정형 문구 반복·고빈도 텍스트·새 계정 전면 자동화)이 신호였을 가능성을 높게 본다. 이것은 추정이다.
- 그래서 v1.6.0 은 '원인 하나를 고치는' 설계가 아니라, 위험 요인 전부를 **기본값에서 끄고** 운영자가 근거를 보며 하나씩 여는 설계로 간다.

### 1-3. 새 계정 관련 주의 (2차 출처)

- 비활성화된 계정의 제재를 피하려고 새 계정을 만드는 행위는 Meta 커뮤니티 규정의 **계정 무결성(Account Integrity)** 정책에서 '집행 회피(evasion)'로 다뤄질 수 있다.
  (출처: Meta Transparency Center — Community Standards, Account Integrity 항목. **2차 출처 요약이며 원문 대조 필요**)
- 이 문서는 법률·정책 자문이 아니다. 새 계정 운영 여부는 운영자가 원문을 확인하고 판단한다. 먼저 기존 계정의 이의 제기(재검토 요청) 경로를 확인하는 것을 권장한다(권장, 사실 아님).

---

## 2. 항목별 변경 (before → after)

### S1 전역 킬 스위치 — `AUTOMATION_ENABLED` (기본 **false**)

| | v1.5.0 | v1.6.0 |
|---|---|---|
| 동작 | 기능별 스위치만 존재(CHAT_ENABLED 등). 정기 발행·답글은 기본 켜짐 | `false` 이면 정기·CHAT·STORY·댓글 답글·이어쓰기 전부 **Threads 쓰기 0건 · Claude 호출 0건** |
| 판정 위치 | — | 각 러너 `run()` 첫머리(`safety.block_reason`) — 설정 검사·토큰·Notion·Claude 호출보다 먼저. `run_reply.sweep` 도 자체 판정(CHAT 경유 호출 포함) |
| 종료코드 | — | 0 (실패 아님). 로그: `AUTOMATION_ENABLED=false — 계정 보호 모드(킬 스위치)…` |
| 수동 dry_run | CHAT 은 `CHAT_ENABLED=false` 여도 수동 dry_run 미리보기 허용 | 킬 스위치·워밍업 중에는 수동 dry_run 도 생성하지 않는다(Claude 호출 금지 요구) |
| 읽기 전용 | — | insights · watchdog · golive_check · reply_audit · token_refresh · verify_token 은 그대로 동작 |

### S2 일일 총량 예산 — `DAILY_POST_BUDGET` (기본 **2**)

- KST 하루 **자동 최상위 게시물**(정기 + CHAT + STORY 합산) 상한. 기능별 상한(CHAT 목표, EVENT_DAILY_CAP 등)은 그대로이며 **둘 중 작은 쪽**이 적용된다.
- 무상태: `GET /{user-id}/threads`(오늘·어제, 최대 25건) 결과를 KST 날짜로 센다. 답글은 이 목록에 들어오지 않는 것으로 보고(별도 엔드포인트), 응답에 `is_reply=true` 가 있으면 방어적으로 뺀다.
  - 앱에서 사람이 직접 올린 글도 세어진다(보수적).
  - timestamp 를 읽지 못한 글은 오늘 글로 센다(형식 변경 시 예산이 무력화되지 않게).
- 판정 시점: 정기 — 쿼터 확인 직후(생성 전) + 발행 전 지연 뒤 재확인. CHAT — 사전 게이트·발행 직전 재게이트(기존 `_check_gate` 안). STORY — `_gate`(사전·지연 후 재검증 모두).

**우선순위 규칙 (CHAT 이 정기 몫을 먹지 않게)**

| 종류 | 허용 조건 |
|---|---|
| 정기(regular) | 오늘 게시물 수 < 예산 |
| CHAT · STORY | 오늘 게시물 수 < 예산, 그리고 **정기 몫 예약 중이면** 오늘 게시물 수 < 예산 − 1 |

정기 몫 예약(`safety.regular_reserved`):
1. 오늘 당첨 정기 슬롯(main 과 같은 `antibot.choose_slot`·솔트) 시각 + 판정 창 `PUBLISH_CLASSIFY_WINDOW_MIN`(47분 = 지연 최대 20 + 여유 27) 까지 예약한다.
2. 그 창 안에 CHAT 이 아닌 글(이미지 또는 창 안 텍스트 폴백)이 있으면 정기 발행 완료로 보고 예약하지 않는다.
3. 창이 지났으면 정기가 실패했더라도 예약을 푼다(남은 예산은 CHAT·STORY 가 쓸 수 있다).
4. 휴식일·슬롯 없음이면 예약하지 않는다.

예) 예산 2, 당첨 슬롯 B 12:26 → 13:13 까지 예약. 09:30 CHAT 1건 후 10:30 CHAT 은 보류, 12:3x 정기는 허용. 정기 후 오늘 2건 → 그날 CHAT·STORY 는 더 없다.
예) 예산 1 → 정기 창이 지날 때까지 CHAT·STORY 는 0건. 정기가 나가면 그날 끝.

### S3 링크 셀프 리플라이 — `LINK_REPLY_PCT` (기본 **0**, 0~100)

| | v1.5.0 | v1.6.0 |
|---|---|---|
| 정기·STORY 글 | 항상 YouTube·X 링크 셀프 리플라이 | `sha256(post_id + "::link") % 100 < PCT` 인 글만. 0 = 전혀 달지 않음, 100 = 항상 |
| 구현 | `main.py`·`run_story.py` 각각 `publish_self_reply` | `main._link_reply` 하나로 통일(run_story 재사용) |
| 성질 | — | 무상태·멱등(같은 ID 는 같은 결과), 비율을 올리면 이전 대상은 계속 대상(단조) |

문구·배치 회전(`REPLY_LEADS`·`REPLY_LAYOUTS`)과 `plan.reply_text` 생성은 그대로 둔다(켜면 이전과 같은 형식).

**링크 리플이 없을 때 영향 점검**

| 대상 | 결과 |
|---|---|
| `run_reply.build_ledger` | 원글 직속 내 글(링크 리플·이어쓰기)은 원래부터 댓글 응답 집계에서 제외 → 링크 유무와 무관하게 같음 (테스트) |
| `run_reply._has_link` / `_my_followups` | 이어쓰기 = '링크 없는 원글 직속 내 글'. 링크 리플이 없으면 오탐 여지가 오히려 줄어듦 (테스트) |
| `reply_engine.prior_reply_texts` | 링크 포함 글만 제외 → 영향 없음 |
| 워치독 답글 활동 | 원글 직속 내 답글은 원래부터 제외 → 영향 없음 |
| 인사이트 | 링크 클릭이 0이면 기존 '링크 클릭 없음' 표시. 게시물별 replies 지표에서 내 링크 리플 1건이 빠질 수 있음(지표 해석 시 참고) |
| `scripts/reply_audit.py` | 원글 직속 내 글 제외 로직 그대로 |
| 쿼터 사전 확인 | `quota.remaining < 2`(본문 + 리플) 그대로 — 보수적 |

### S4 답글량 축소 — 새 안전 기본값 (Variables 로 덮어쓰기 가능)

| 키 | v1.5.0 | v1.6.0 |
|---|---|---|
| `REPLY_DAILY_CAP` | 40 | **10** |
| `REPLY_AUTHOR_DAILY_CAP` | 3 | **1** |
| `REPLY_THREAD_AUTHOR_CAP` | 4 | **2** |
| `REPLY_PER_RUN_CAP` (CHAT 스윕) | 4 | **2** |
| `REPLY_SCHEDULED_RUN_CAP` (reply.yml) | 6 | **3** |
| `REPLY_CANNED_ENABLED` (신규) | (항상 정형 문구) | **false** — 외국어 댓글은 SKIP(답하지 않음, 캡 소모 없음) |

워크플로 기본값 식(`vars.X || 'N'`)을 chat · reply · golive_check · reply_audit 에서 모두 새 값으로 바꿨다. `REPLY_PER_RUN_CAP`·`REPLY_SCHEDULED_RUN_CAP` 은 빈 값에 안전한 `_int_env` 로 바꿨다.

### S5 워밍업 — `WARMUP_UNTIL` (KST `YYYY-MM-DD`, 기본 빈 값 = 없음)

오늘(KST) ≤ WARMUP_UNTIL 이면:

| 항목 | 워밍업 중 |
|---|---|
| 일일 예산 | min(DAILY_POST_BUDGET, 1) |
| 링크 리플 비율 | 0 |
| 댓글 답글 · 이어쓰기 | 중지 |
| CHAT · STORY | 중지 |
| 정기 발행 | 하루 1건만 허용 |

- 빈 값·자리표시자(`—`, `-`, `none`, `없음` 등)는 '워밍업 없음'.
- 형식이 `YYYY-MM-DD` 가 아니거나 날짜로 읽히지 않으면 **워밍업으로 본다(fail safe)** + 경고 로그 1회. (`date.fromisoformat` 이 받는 `20261001` 같은 축약형도 거부)

### S6 이어쓰기 — `FOLLOWUP_ENABLED` 기본 false 유지

`FOLLOWUP_ENABLED=true` 여도 S1(킬 스위치)·S5(워밍업)에 막힌다(`safety.followups_allowed`).

### S7 계정 제한 회로 차단기

| 항목 | 내용 |
|---|---|
| 대상 오류 | `ThreadsApiError.is_blocked`(code 200 또는 본문 'access blocked') · `ThreadsApiError.is_auth_error`(code 190 또는 HTTP 401). 기존 클래스가 이미 다루는 두 분류만 쓴다 |
| 가정 | code 190 = OAuthException(토큰 무효·만료·권한 박탈), code 200 = 권한/접근 차단으로 본다(기존 코드·운영 기록 기준). **code 368(정책 위반 일시 차단)·10(권한) 등은 이번에 넣지 않았다** — 현행 코드에서 처리·관측된 근거가 없어서다(미결 C3) |
| `threads_client._request` | 대상 오류면 상태 코드와 무관하게 **재시도 없이** 즉시 올리고 차단기를 연다(이전: code 200 이 5xx 로 오면 3회 재시도) |
| 쓰기 가드 | `_create_container`·`publish` 직전 `safety.guard_write()` — 차단기가 열렸으면 원래 오류를 다시 올린다. 조회 실패를 삼키는 경로(`get_recent_texts`)가 있어도 쓰기가 새지 않는다. 생성(Claude) 전에도 확인 |
| 러너 | 이미지 실패 → 텍스트 폴백(쓰기 재시도) 금지. 답글·이어쓰기 건별 실패로 넘기지 않고 즉시 전파. CHAT 스윕에서도 190 을 삼키지 않음 |
| 알림·종료 | 러너 `main()` 에서 `safety.handle_fatal` — 텔레그램 1회, 종료코드 **7** |
| 변경된 동작 | 토큰 무효(190/401)의 종료코드 4 → **7** (정기·CHAT). 알림 문구에 재인가 안내 유지 |

### S8 golive_check C13

자동화 on/off · 워밍업 상태 · 예산(설정→적용) · 링크 비율(설정→적용) · 답글 캡 · 정형 문구 · 기능별 허용을 표시한다.
`AUTOMATION_ENABLED=true` 이면서 `LINK_REPLY_PCT>0` · `REPLY_DAILY_CAP>10` · `DAILY_POST_BUDGET>3` 이면 **WARN**(안전 프로필 초과). FAIL 은 하지 않는다. 워밍업 날짜 형식 오류도 WARN.

### 워치독 오탐 방지

| 검사 | 자동화 꺼짐 | 워밍업 | 예산 제한 |
|---|---|---|---|
| 발행 신선도 | OK "발행 판정 생략" | 글 0건이면 OK "발행 이력 없음(워밍업)". 글이 있으면 기존 판정(정기는 매일 돈다) | 예산 0 → OK |
| CHAT 무발행 | OK + 사유 | OK + 사유 | 적용 예산 < 1 + 오늘 CHAT 목표 → OK + 사유(기본 예산 2 에서는 항상 생략) |
| 답글 활동 | OK + 사유 | OK + 사유 | — |
| API 접근 실패 | 기존대로 CRITICAL(차단 감지는 유지) | 같음 | 같음 |

---

## 3. 기본값 표

| Variable | v1.5.0 | v1.6.0 기본 | 워크플로 식 |
|---|---|---|---|
| `AUTOMATION_ENABLED` | (없음 — 사실상 켜짐) | `false` | `${{ vars.AUTOMATION_ENABLED \|\| 'false' }}` |
| `DAILY_POST_BUDGET` | (없음 — 무제한) | `2` | `${{ vars.DAILY_POST_BUDGET \|\| '2' }}` |
| `LINK_REPLY_PCT` | (없음 — 100%) | `0` | `${{ vars.LINK_REPLY_PCT \|\| '0' }}` |
| `WARMUP_UNTIL` | (없음) | 빈 값 | `${{ vars.WARMUP_UNTIL }}` |
| `REPLY_CANNED_ENABLED` | (없음 — 항상 사용) | `false` | `${{ vars.REPLY_CANNED_ENABLED \|\| 'false' }}` |
| `REPLY_DAILY_CAP` | 40 | 10 | `'10'` |
| `REPLY_AUTHOR_DAILY_CAP` | 3 | 1 | `'1'` |
| `REPLY_THREAD_AUTHOR_CAP` | 4 | 2 | `'2'` |
| `REPLY_PER_RUN_CAP` | 4 | 2 | `'2'` |
| `REPLY_SCHEDULED_RUN_CAP` | 6 | 3 | `'3'` |
| `FOLLOWUP_ENABLED` | false | false (S1·S5 추가 차단) | 변경 없음 |

워크플로별 전달(verify_repo 검사 11 `SAFETY_WORKFLOW_KEYS`):

| 워크플로 | AUTOMATION | BUDGET | LINK_PCT | WARMUP | CANNED | 답글 캡 |
|---|---|---|---|---|---|---|
| publish.yml | ✅ | ✅ | ✅ | ✅ | | |
| story.yml | ✅ | ✅ | ✅ | ✅ | | |
| chat.yml | ✅ | ✅ | | ✅ | ✅ | 일·저자·스레드·실행당 |
| reply.yml | ✅ | | | ✅ | ✅ | 일·저자·스레드·예약 실행당 |
| watchdog.yml | ✅ | ✅ | | ✅ | | |
| golive_check.yml | ✅ | ✅ | ✅ | ✅ | ✅ | 전부 |
| reply_audit.yml | | | | | ✅ | 저자·스레드 |

검사 11 은 (a) `config.SAFETY_VARIABLE_DEFAULTS` = config 실제 기본값, (b) 표의 키 누락 없음, (c) **모든 워크플로**에서 이 키들의 식이 표준 식인지 본다.

---

## 4. 새 계정 운영 절차 (권장 — 사실 아님)

아래는 위험을 줄이기 위한 **권장 절차**다. 효과는 검증되지 않았다(Meta 기준 비공개).

1. 배포 직후: Variables 에 아무것도 넣지 않는다 → `AUTOMATION_ENABLED=false`(기본). 모든 쓰기 경로가 멈춘 상태로 읽기 워크플로만 돈다.
2. 계정은 한동안 **사람이 앱에서 직접** 사용한다(프로필 정리, 수동 글, 다른 사용자 글 읽기·반응 등). (권장)
3. Go-Live Check 실행 → C13 에서 적용값 확인.
4. 처음 자동화를 켤 때: `WARMUP_UNTIL` 을 오늘부터 약 2~4주 뒤로 먼저 넣고(예: `2026-10-31`), 그다음 `AUTOMATION_ENABLED=true`. 워밍업 동안은 정기 1건/일·링크 없음·답글/CHAT/STORY 없음.
5. 워밍업이 끝나면 기본(예산 2·링크 0%·답글 일 10·정형 문구 꺼짐)으로 운영한다. CHAT·STORY 는 기능별 스위치(`CHAT_ENABLED`·`EVENT_STORY_ENABLED`)도 켜야 나간다.
6. 값을 올릴 때는 한 번에 하나씩, 1~2주 간격으로. golive_check 의 '안전 프로필' WARN 을 확인한다.
7. 계정 경고·잠금 징후가 있으면 즉시 `AUTOMATION_ENABLED=false`.

## 5. 롤백

| 목적 | 방법 |
|---|---|
| 즉시 전체 정지 | Variables `AUTOMATION_ENABLED=false` (코드 배포 불필요) |
| v1.5.0 동작에 가깝게 | `AUTOMATION_ENABLED=true`, `DAILY_POST_BUDGET=1000`, `LINK_REPLY_PCT=100`, `REPLY_CANNED_ENABLED=true`, `REPLY_DAILY_CAP=40`, `REPLY_AUTHOR_DAILY_CAP=3`, `REPLY_THREAD_AUTHOR_CAP=4`, `REPLY_PER_RUN_CAP=4`, `REPLY_SCHEDULED_RUN_CAP=6`, `WARMUP_UNTIL` 삭제 (권장하지 않음 — 계정 비활성화 당시 프로필) |
| 코드 롤백 | v1.5.0 커밋(f36257a)으로 되돌린다. 단 S7(토큰 무효 시 재시도·폴백 금지)도 함께 사라진다 |

v1.6.0 에서도 남는 v1.5.0 와의 차이(롤백 Variables 로 복원되지 않는 것): 토큰 무효 종료코드 7, code 200 5xx 무재시도, 회로 차단 후 쓰기 금지, 수동 dry_run 도 킬 스위치에 막힘.

---

## 6. 미결

| # | 항목 |
|---|---|
| C1 | 예산 집계는 최근 25건까지만 본다. `DAILY_POST_BUDGET` ≥ 25 는 의미가 없다(권장 상한 3) |
| C2 | 사람이 앱에서 올린 글도 예산에 세어진다(보수적). 수동 글이 많은 날은 자동 발행이 줄어든다 |
| C3 | code 368 등 정책 위반 일시 차단 코드는 회로 차단 대상이 아니다. 실제 응답이 관측되면 근거와 함께 추가 |
| C4 | `GET /threads` 에 답글이 섞이지 않는다는 것은 엔드포인트 구분에 따른 가정이다. 섞이면 `is_reply` 필드가 응답에 있어야 걸러진다(현재 요청 필드에 `is_reply` 미포함) |
| C5 | 자동화를 오래 끈 뒤 다시 켜면 워치독이 첫 정기 발행 전까지 '발행 장기 중단'을 낼 수 있다(워밍업 중이면 글 0건일 때만 생략) |
