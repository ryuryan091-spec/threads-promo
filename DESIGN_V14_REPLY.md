# v1.4.0 답글 엔진 고도화 (Reply Engine)

상태 **구현·테스트 완료 · 배포 대기** · 작성 2026-10-02
기준 코드 v1.3.0 (pytest 761 passed) · 선행 문서 `DESIGN_V13_HUMANIZE`, `DESIGN_CHAT_REPLY`

---

## 0. 요약

| # | 결함·요구 | 변경 | 결과 |
|---|---|---|---|
| R0 | 실제 답글 품질을 볼 수단 없음 | 읽기 전용 감사 도구 `scripts/reply_audit.py` + `reply_audit.yml`(수동 실행) | 원글·댓글·내 답글·현재 로직 재판정을 Actions 요약 표로 |
| R1 | 'ㅋㅋㅋㅋ'·'ㄹㅇ' 이 비한국어로 판정 → 외국어 정형 문구 발송 | 한글 호환 자모(U+3131–U+318E)를 한국어로 셈 + **REACTION** 전략 신설 | 자모·짧은 리액션 → AI 한 마디(25자 이내, 물음표 없음) |
| R2 | 선택형 패턴이 넓어 맥락 없는 정형 문구("둘 다 상황에 따라…") 발송 | 선택형 정형 문구 **폐지** → AI 생성 + 힌트(어느 쪽도 고르지 않음 / 투자면 판단 유보) | 고정 판단 유보 문장 제거 |
| R3 | 외국어 문구 3종, 같은 대화에서 반복 가능 | 7종으로 확대 + 같은 글 대화에서 이미 쓴 문구 제외(무상태) | 모두 소진되면 응답 생략 |
| R4 | 답글 형식(길이·되묻기)이 댓글 ID 해시로만 결정 | 댓글 내용으로 범주 결정 → 범주 안에서만 해시 변주 | 질문엔 답 먼저·되묻기 없음, 짧은 댓글엔 한 마디 |
| R5 | 맥락이 원글 300자 + 직전 답글 200자뿐 | replied_to 사슬 최대 4턴, 원글 600자·턴 300자, 내 답글 최대 5건 | 대댓글 흐름 유지, 첫마디 반복 억제 |
| R6 | 답글에는 반복 린트 없음 | 같은 글 내 답글 + 이번 실행 답글과 반복이면 재생성, **마지막 시도도 반복이면 생략** | 원글(마지막 시도 채택)과 의도적으로 다름 |
| R7 | 프롬프트 길이 기준 3중(100자/두 문장/200자) | 길이 기준은 형식 블록 하나, 존댓말 유지·가벼운 댓글엔 가벼운 말투, 좋은/나쁜 예시 3쌍 | 금지·사실 제약 원문 유지 |
| R8 | — | `tests/test_reply_v14.py` 102건 | pytest 864 passed |

## 1. 마스터 결정 사항

| # | 결정 | 적용 |
|---|---|---|
| D1 | REACTION 은 AI 가 아주 짧게(한 마디, ≤ 25자, 질문 없음), 존댓말. AI 불가·실패면 생략 | `compose` 가 25자 초과·물음표를 재생성 사유로 처리, 끝내 실패하면 None |
| D2 | 반복 린트 마지막 시도까지 반복이면 **답글 생략**(발행 안 함), 사유 로그 | `compose` → `WARNING 답글 반복 — 마지막 시도(2/2)라 답글 생략` |
| D3 | 존댓말 유지(반말 없음), 가벼운 댓글엔 가벼운 말투 허용 | `REPLY_SYSTEM_PROMPT` 답글 원칙 |

## 2. 판정 흐름 (decide)

```
SKIP 게이트(내 글·제3자 대화·이미 답글·숨김·2자 미만·문자 없음·저자 캡·스레드 캡)  ← 변경 없음
  → 비한국어?        NON_KOREAN      정형 문구(이미 쓴 것 제외)
  → 리액션?          REACTION        AI 한 마디
  → 선택형 패턴?     NEUTRAL_THANKS  AI + 선택형 힌트 (+ 투자 힌트)   ※ 값 'neutral' 은 하위 호환으로 유지
  → 그 외            NORMAL          AI (+ 투자 힌트)
```

- 리액션 규칙(보수적): 문자(공백·기호·이모지 제외)가 전부 자모면 리액션(길이 무관). 아니면 문자 12자 이하이고,
  자모를 뺀 나머지가 리액션 어휘(굿·굳·대박·화이팅·파이팅·최고·짱·인정·와·우와·오·헐·멋져요·멋지네요·좋아요·좋네요)의
  반복·조합으로만 이루어질 때만. 다른 단어가 하나라도 섞이면 일반.
- '굿' 처럼 1자 댓글은 기존 규칙(2자 미만 생략)이 우선한다.
- 투자 힌트는 어휘(투자·주식·ETF·코인·매수…) 포함 시 붙는다. 넓게 잡아도 지시가 덧붙을 뿐 전략은 바뀌지 않는다.

## 3. 답글 형식 범주 (style.pick_reply_style)

| 범주 | 조건(우선순위 순) | 길이(해시 변주) | 되묻기 |
|---|---|---|---|
| reaction | REACTION 전략 | react(25자) | 없음 |
| question | 물음표 또는 의문 어미(까요·나요·는지·궁금…) | one 60 / two 40 | 없음 |
| short | 문자 ≤ 12 | tiny(40자) | 없음 |
| long | 문자 ≥ 60 | one 45 / two 55 | 35% |
| normal | 그 외 | tiny 35 / one 65 | 35% |

- 길이 기준(react 25 · tiny 40 · one 80 · two 150자)은 `style.REPLY_LENGTH_LIMITS` 한 곳. 시스템 프롬프트는 이를 참조만 한다.
- 강제(재생성 사유)는 react 25자뿐. `REPLY_MAX_LEN`(200)은 린트 절대 상한(안전망)으로 유지.

## 4. 맥락·반복 (run_reply → compose)

- `build_dialogue`: 새 댓글의 replied_to 를 대화 목록 안에서 거슬러 올라가 최대 `REPLY_CONTEXT_TURNS`(4)턴, 오래된 순.
  내 답글은 그대로, 타인 글은 `<<< >>>` 데이터 블록(작성자 본인/다른 사람 구분). 순환 참조는 방문 집합으로 차단.
- 반복 판정 목록 = 이번 실행에서 발행(또는 DRY_RUN 계획)한 답글(최신순) + 같은 글의 내 답글(최신순, `http` 포함 링크 리플 제외).
  같은 목록 앞 5건을 프롬프트 `# 이미 쓴 답글 (첫마디·끝맺음 반복 금지)`로 넘긴다(모델이 린트 대상을 보고 피하도록).
  ※ 이번 실행분은 다른 글의 답글도 포함한다(계정 단위 말투 반복 억제).
- 발행 실패한 답글은 목록에 넣지 않는다(실제로 보이지 않음).
- 판정은 기존 `content.check_repetition`(첫 어절 일치 / 끝맺음 3자 동일 3건 이상) 재사용, `AI_MAX_RETRY`(2) 안에서 재생성.

## 5. R0 답글 감사

- 실행: Actions → **Threads Reply Audit** → days(기본 3, 1~14로 보정).
- 토큰: `THREADS_LONG_LIVED_TOKEN` Secret 그대로(갱신·저장 없음), 사용자 ID 는 `THREADS_USER_ID` 또는 GET /me.
- 호출: GET /me, GET 내 글 목록, GET 대화만. Claude·발행 호출 없음. `permissions: contents: read`.
- 출력: 원글 시각(KST)·앞부분 / 작성자(앞 2글자 + ***) / 댓글 원문(@언급 가림) / 내 답글 / 재판정(방침·사유) / 형식 범주.
  재판정은 캡·'이미 답글함'을 제외한 현재 `decide` 결과.

## 6. 영향 파일

```
src/reply_engine.py   1.4.0  REACTION·자모·투자 힌트·대화 사슬·프롬프트 정리·외국어 문구 7종·반복 생략
src/run_reply.py      1.4.0  ReplyPlan, 대화 사슬·이미 쓴 답글·실행 내 반복·정형 문구 중복 회피 연결
src/style.py          1.1.0  내용 기반 답글 범주(reply_kind·is_question), 길이 기준 단일화
src/config.py         (버전 유지) REPLY_CONTEXT_* · REPLY_PRIOR_REPLIES_MAX · REPLY_REACTION_* ·
                      REPLY_SHORT/LONG_COMMENT_CHARS · REPLY_LENGTH_WEIGHTS_BY_KIND (REPLY_LENGTH_WEIGHTS 대체)
src/redact.py         1.1.0  mask_username · mask_mentions
scripts/reply_audit.py        신규 (읽기 전용)
scripts/verify_repo.py        REQUIRED_FILES 에 reply_audit.yml · reply_audit.py 등록 (검사 로직 변경 없음)
.github/workflows/reply_audit.yml  신규 (workflow_dispatch 전용)
tests/test_reply_v14.py       신규 102건
tests/ (기존 5개 파일)         명세 변경 반영 — 아래 7절
REQUIREMENTS.md               FR-44 문구 갱신
```

새 Variables 없음 → README Secrets/Variables 표 변경 없음.

## 7. 기존 테스트 변경 (의도된 동작 변경)

| 파일 | 테스트 | 사유 |
|---|---|---|
| test_reply_e2e.py | test_choice_comment_gets_neutral (+ 키 없음 생략 1건 추가) | 선택형 정형 문구 폐지 → AI 생성 |
| test_reply_and_antibot.py | TestCanned.test_within_pool | `NEUTRAL_THANKS_REPLIES` 삭제 → 외국어 풀 + exclude |
| test_humanize_v13.py | test_reply_style, test_canned_replies_pass_lint | 형식 범주가 내용 기반 / 선택형 풀 삭제 |
| test_reply_stateless_caps.py | `_sweep` 모의 생성값 | 같은 실행 안 같은 답글 반복은 생략(R6) → 서로 다른 모의 답글 |
| test_enhance_v12.py | `_budget_sweep` 모의 생성값 | 같은 사유 |

## 8. 남은 확인 사항

- 의문 어미·리액션 어휘는 휴리스틱이다. 실제 댓글 분포는 R0 감사 결과로 보정한다.
- `AI_MAX_RETRY`=2 라 반복 시 재생성 기회는 1회다. 감사에서 '반복 생략'이 잦으면 재시도 횟수 또는 판정 기준 조정 검토.
- 선택형 패턴 자체는 넓은 그대로다(이제 힌트일 뿐이고 힌트 문구가 '골라달라는 질문으로 보이면'으로 조건부).
