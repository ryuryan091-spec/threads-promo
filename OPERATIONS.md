# threads-promo 운영자 매뉴얼

버전 1.0 · 최종 갱신 2026-09-12
대상 레포: `ryuryan091-spec/threads-promo` · 발행 계정: `@ryuryan091`

---

## 0. 한 장 요약

| 항목 | 내용 |
|---|---|
| 목적 | Threads로 YouTube·X 유입 (Threads 자체 수익 없음) |
| 발행 | 매일 1회, 3개 슬롯 중 랜덤 1개 (KST 08:23 / 12:47 / 20:31) |
| 답글 | 매일 1회, 3개 슬롯 중 랜덤 1개 (KST 10:19 / 16:53 / 22:07) |
| 상태 저장 | 없음(무상태). Secret 1개만 상태 |
| 사람 개입 | 평시 0. 60일마다 토큰 확인 |
| 비용 | Threads 0원, Actions 0원(Public), Claude API만 종량 |

---

## 1. 시스템 구성

```
[GitHub Actions]
   ├─ publish.yml   일 3회 트리거 → 1회만 실행 → 이미지+본문 발행 → 셀프리플라이(링크)
   ├─ reply.yml     일 3회 트리거 → 1회만 실행 → 내 글 댓글에 답글
   ├─ verify_token.yml   수동. 토큰 검증 + USER_ID 조회
   ├─ auth_url.yml       수동. OAuth 인가 URL 생성 (예비)
   └─ bootstrap.yml      수동. OAuth 토큰 발급 (예비)

[모듈]
   config.py        상수·금칙어·상한
   env.py           환경변수 로딩·검증
   ai_writer.py     콘텐츠 기둥, Claude 호출, 프롬프트
   content.py       기둥·소재 선택, 생성/폴백, 린트
   reply_engine.py  댓글 판정, 답글 생성, 정형 문구
   antibot.py       슬롯 판정, 랜덤 지연, 상한
   threads_client.py  Threads API 래퍼
   token_manager.py   토큰 갱신·Secret 영속화
   notifier.py      텔레그램 실패 알림
   main.py          발행 엔트리
   run_reply.py     답글 엔트리
```

---

## 2. 일상 운영

### 평시에 할 일

**없습니다.** 자동으로 돕니다. 아래 두 가지만 확인하십시오.

| 신호 | 의미 | 조치 |
|---|---|---|
| 텔레그램 알림 도착 | 실패 발생 | 5장 런북 참조 |
| Actions 탭 빨간 X | 실패 발생 | 로그 확인 후 런북 |

### 수동 실행

```
Actions → Threads Publish → Run workflow
  mode: dry_run   (검증만, 발행 안 함)
  mode: live      (실제 발행)

Actions → Threads Reply → Run workflow
  mode: dry_run / live
```

**변경 후에는 반드시 dry_run을 먼저 돌립니다.**

### 로그 읽는 법

정상 발행 로그:

```
[Publish] v1.1.0 시작
토큰 갱신 완료. 남은 유효기간 약 59일
사용자 ID 조회 완료 — @ryuryan091 (id=38808359068777315)
발행 쿼터 0/250 (잔여 250)
중복 회피용 최근 글 8건 확보
기둥=BUILD 소재=새벽에 고친 버그 생성=ai 이미지=https://raw.githubusercontent.com/...
랜덤 지연 213초 (발행 전)
발행 완료 id=...
```

확인 포인트 4개:

| 로그 | 정상값 |
|---|---|
| `남은 유효기간` | 55일 이상 |
| `생성=` | `ai` (`static`이면 Claude 호출 실패) |
| `이미지=` | `raw.githubusercontent.com`으로 시작 |
| `쿼터` | 잔여 240 이상 |

미당첨 슬롯:

```
슬롯 판정 — 오늘 선택=B 현재=A 실행=아니오
오늘 슬롯이 아님 — 종료
```

이것은 **정상**입니다. 실패가 아닙니다.

---

## 3. 정기 점검

| 주기 | 항목 | 방법 | 이상 시 |
|---|---|---|---|
| 주 1회 | Threads Account Status | Threads 앱 → 설정 → 계정 상태 | 강등 표시 시 6-2 참조 |
| 주 1회 | 답글 수 추이 | 내 글 댓글 수 | 0에 수렴하면 콘텐츠 재설계 |
| 주 1회 | 발행 성공률 | Actions 탭 | 실패 반복 시 런북 |
| 월 1회 | YouTube 구독자 증감 | YouTube Studio | 유입 효과 실측 |
| 월 1회 | 토큰 잔여일 | 발행 로그 | 55일 미만이면 4-1 확인 |
| 월 1회 | Claude 사용량 | Anthropic Console | 예산 초과 시 7장 |

**핵심 지표는 조회수가 아니라 답글 수입니다.** Threads 도달이 답글로 결정되기 때문입니다.

---

## 4. 토큰 생명주기

### 4-1. 정상 상태

`publish.yml`이 매 실행마다 토큰을 갱신하고 새 값을 Secret에 되씁니다. 갱신 가능 구간은 **발급 후 24시간 ~ 60일**이므로, 매일 도는 한 만료되지 않습니다.

로그에 `토큰 갱신 완료. 남은 유효기간 약 59일`이 매일 찍히면 정상입니다.

### 4-2. 위험 신호

| 로그 | 의미 | 조치 |
|---|---|---|
| `Secret 갱신 실패` | PAT 문제. **갱신값이 저장되지 않음** | PAT 만료·권한 확인 |
| `공개키 조회 실패 401 Bad credentials` | PAT 미등록·만료·값 손상 | PAT 재발급 후 Secret 교체 |
| `[Threads][경고] 토큰 영속화 실패` | 잔여 20일 이하 | PAT 복구 또는 토큰 재발급 |
| `[Threads][긴급]` | 잔여 10일 이하 | 재발급 서두를 것 |
| `[Threads][최우선]` | 잔여 3일 이하 | 당일 재발급 |
| `[Threads][확인필요]` | `TOKEN_ISSUED_AT` 미설정 | Variable 등록 |
| `남은 유효기간 약 30일` 이하 | 갱신이 며칠째 저장 안 됨 | 즉시 PAT 확인 |
| `토큰 갱신 실패 — 기존 토큰으로 진행` | 갱신 API 실패 | 1~2회는 무시. 반복 시 재발급 |

**PAT가 만료되면 조용히 실패합니다.** 발행은 계속되지만 갱신값이 저장되지 않아 60일 후 전면 정지합니다. 월 1회 잔여일 확인이 이 사고를 막습니다.

### 4-3. 토큰 재발급 (60일 만료 또는 강제 재발급)

```
1. Meta 콘솔 → 이용 사례 → Threads API 액세스 → 맞춤 설정 → 설정
2. 최하단 "사용자 토큰 생성기" → ryuryan091 → 액세스 토큰 생성하기
3. 값 복사 (창 닫기 전에)
4. GitHub → Settings → Secrets → THREADS_LONG_LIVED_TOKEN → Update
5. Actions → Verify Token → Run workflow 로 검증
```

**전제**: `ryuryan091` 계정이 공개 상태여야 생성 버튼이 동작합니다.

OAuth 경로(`auth_url.yml` → `bootstrap.yml`)는 예비로 남겨둔 것입니다. 본인 계정만 쓰는 한 사용할 일이 없습니다.

---

## 5. 장애 대응 런북

### 5-1. 오류 코드 사전

실제로 겪은 오류를 원인별로 정리했습니다.

| 코드 | 메시지 | 원인 | 조치 |
|---|---|---|---|
| `190` | Cannot parse access token | Secret 값이 토큰이 아님 | 값 확인: `THAA` 시작, 100자 이상, 따옴표·공백 없음 |
| `190` | Invalid OAuth 2.0 Access Token | 토큰 만료 또는 다른 앱 발급 | 4-3으로 재발급 |
| `100` / subcode 33 | Object with ID '...' does not exist | `THREADS_USER_ID` 오류 | Secret 삭제 → 런타임 자동 조회 |
| `36001` / subcode 2207083 | 이미지 포맷 인식 불가 | URL이 이미지가 아니거나 JPEG/PNG 아님 | 실행 시 출력되는 `진단:` 줄 참조 |
| exit 5 | 이미지 검증 실패 | 발행 전 사전 차단 | `진단:` 줄이 원인을 지목함 |
| `24` / subcode 4279009 | Media Not Found | 컨테이너 처리 완료 전 발행 | 자동 대기·폴링으로 해소됨. 재발 시 `CONTAINER_WAIT_IMAGE_SEC` 상향 |
| exit 6 | 컨테이너 처리 실패 | ERROR/EXPIRED/타임아웃 | 로그의 `error_message` 확인 |
| `1349245` | 테스트 초대 미수락 | Threads Tester 수락 안 함 | Threads 앱 → 설정 → 웹사이트 권한 → 초대 → 수락 |
| `1349168` | 리디렉션 URI 화이트리스트 없음 | 콜백 URL 미등록 | Meta 콘솔 → 설정 → 리디렉션 콜백 URL |
| `4476001` | URI에 리디렉션이 없습니다 | `redirect_uri` 파라미터 누락·손상 | `https://` 로 시작하는지 확인 |
| `4476002` | 앱 ID가 전송되지 않았습니다 | `client_id` 누락 | 인가 URL을 한 줄로 복사 |

### 5-2. 증상별 대응

**수동 실행했는데 `현재=MANUAL 실행=아니오`로 종료됨**

v1.1 이전 버전입니다. `EVENT_NAME` 주입 패치를 적용하십시오. 수동 실행은 슬롯과 무관하게 항상 실행되어야 합니다.

**슬롯 불일치 오류 발생**

```
슬롯 'MANUAL' 이 등록 목록 ['A', 'B', 'C'] 에 없습니다.
```

스케줄 실행인데 슬롯이 매핑되지 않았다는 뜻입니다. cron 문자열과 `Resolve slot`의 `case` 분기가 어긋났습니다. 이 상태에서는 발행하지 않고 중단합니다(3회 중복 발행 방지).

**발행이 안 됨 — Actions는 성공(초록)**

슬롯 미당첨입니다. 로그에 `오늘 슬롯이 아님`이 있으면 정상이고, 다른 슬롯에서 발행되었을 것입니다. Actions 탭에서 같은 날 다른 실행을 확인하십시오.

**본문이 매번 비슷함**

로그의 `생성=static`을 확인하십시오. Claude 호출이 실패해 정적 텍스트 풀(9건)만 돌고 있는 상태입니다.

| 원인 | 확인 |
|---|---|
| `CLAUDE_AI_KEY` 미등록·오류 | 로그의 `API 401` |
| 크레딧 소진 | 로그의 `API 400` + credit 메시지 |
| 린트 반복 실패 | 로그의 `생성문 린트 실패` |

**이미지가 안 올라감**

이미지 URL을 브라우저에 직접 넣어 보십시오. 안 보이면 Threads도 못 가져옵니다.

| 원인 | 조치 |
|---|---|
| `ASSET_RAW_BASE_URL` Variable에 잘못된 값 | 삭제 (자동 조립됨) |
| `assets/` 비어 있음 | 이미지 커밋 |
| 규격 위반 | JPEG/PNG, 8MB 이하, 폭 320~1440px, 종횡비 10:1 이내, sRGB |

**답글이 안 달림**

| 확인 순서 | 내용 |
|---|---|
| 1 | 로그에 `응답 후보 0건` → 새 댓글이 없거나 전부 스킵됨 |
| 2 | 로그에 `AI 키 없음 — 일반 댓글 응답 생략` → 키 등록 |
| 3 | scope 부족 → `threads_read_replies`, `threads_manage_replies` 필요. 없으면 토큰 재발급 |
| 4 | 로그에 `저자 일일 캡` → 정상 동작 |

**로그의 숫자가 `***`로 가려짐**

Secret 값과 일치하는 문자열이 자동 마스킹된 것입니다. `1`, `me` 같은 짧은 값을 Secret에 넣으면 로그 전체가 오염됩니다. 해당 Secret을 삭제하거나 값을 바꾸십시오. **실제로 디버깅을 몇 시간 지연시킨 사고입니다.**

### 5-3. 긴급 정지

| 대상 | 방법 |
|---|---|
| 발행만 중지 | Variables → `DRY_RUN` = `true` |
| 답글만 중지 | Variables → `REPLY_ENABLED` = `false` |
| AI 생성만 중지 | Variables → `AI_ENABLED` = `false` (정적 텍스트로 동작) |
| 전체 중지 | Actions 탭 → 해당 워크플로우 → `Disable workflow` |

---

## 6. 설정 변경 가이드

### 6-1. 자주 바꾸는 값

| 목적 | 위치 | 값 |
|---|---|---|
| 유입 링크 변경 | Variables | `YOUTUBE_URL`, `X_URL` |
| 홍보 비중 | `src/ai_writer.py` | `PILLAR_ROTATION` 배열 |
| 답글 상한 | `src/config.py` | `REPLY_DAILY_CAP` (기본 20) |
| 저자별 상한 | `src/config.py` | `REPLY_AUTHOR_DAILY_CAP` (기본 2) |
| 지연 범위 | `src/config.py` | `ANTIBOT_*_JITTER` |
| 발행 시각 | `.github/workflows/publish.yml` | cron 3개 + slot 매핑 |
| 주간 휴식일 | Variables | `PUBLISH_WEEKLY_REST_DAYS` (0=매일, 1=주 6회) |
| 모델 | Variables | `CLAUDE_MODEL` |

**링크는 코드가 아니라 Variables에서 바꿉니다.** 코드 편집 중 따옴표 오류로 두 번 장애가 났던 이력이 있습니다.

### 6-2. 콘텐츠 비중 조정

현재 8일 주기:

```python
PILLAR_ROTATION = ("BUILD", "MARKET", "PROMO", "STORY",
                   "BUILD", "MARKET", "PROMO", "STORY")
```

| 상황 | 조정 |
|---|---|
| 답글이 안 붙음 | `PROMO`를 줄이고 `BUILD`를 늘림 |
| 계정 강등 표시 | `PROMO`를 1개로 줄임 |
| 유입이 전혀 없음 | `PROMO`를 3개로. **단 도달이 함께 떨어짐** |

홍보를 늘리면 방송형으로 분류되어 도달이 떨어지고, 결국 홍보 효과도 감소합니다. 늘리는 방향은 신중하게 판단하십시오.

### 6-3. cron 시각 변경 시 주의

cron 문자열과 `Resolve slot` 스텝의 `case` 분기가 **정확히 일치**해야 합니다. 불일치하면 슬롯이 `MANUAL`로 떨어져 매번 실행됩니다.

정각·반각(`00`, `30`)은 피하십시오. 혼잡 시간대는 실제 실행률이 떨어집니다.

---

## 7. 비용 관리

| 항목 | 현재 | 조건 |
|---|---|---|
| Threads API | 0원 | 무제한 |
| GitHub Actions | 0원 | **Public 레포 유지 시**. Private 전환 시 월 390~630분 소모 (한도 2,000분) |
| GitHub Pages | 0원 | Public 레포 |
| 이미지 호스팅 | 0원 | 레포 raw URL |
| DB | 0원 | 미사용 |
| Claude API | 종량 | 아래 |

### Claude 사용량

| 호출 | 빈도 | 통제 |
|---|---|---|
| 발행 본문 | 일 1~2회 | `AI_ENABLED=false`로 차단 |
| 답글 | 댓글 수만큼, 최대 20 | `REPLY_DAILY_CAP` |

댓글이 적은 초기에는 일 1~3회로 미미합니다. 답글이 상한까지 차면 일 20회가 실질 비용 구간입니다.

**Private 전환 시** Actions 분이 과금 대상이 되고 GitHub Pages도 유료 플랜이 필요할 수 있습니다. 전환 전 재검토하십시오.

---

## 8. 금지 사항

| 금지 | 이유 |
|---|---|
| 유료 멤버십·유료 구독 개설 | 자본시장법 제101조 유사투자자문업 신고 의무 발생. 미신고 시 1년 이하 징역 또는 3천만원 이하 벌금 |
| 종목명·목표가·매매권유 문구 | 위와 동일. 금칙어 린트가 차단하나 수동 발행 시 주의 |
| 짧은 값(`1`, `me`)을 Secret에 등록 | 로그 전체 마스킹 오염 |
| 타인 글에 자동 답글 | 스팸 판정 위험 최고 |
| 같은 문구·같은 시각 반복 | Threads 강등 사유 |
| 토큰을 워크플로우 입력란에 붙여넣기 | 실행 기록에 평문 노출 |
| 링크를 `config.py`에서 직접 편집 | 문법 오류 장애 이력 |

---

## 9. 설정값 대장

### Secrets

| 이름 | 필수 | 형식 | 비고 |
|---|---|---|---|
| `THREADS_APP_ID` | ✅ | 16자리 숫자 | Threads 전용. 일반 Meta App ID 아님 |
| `THREADS_APP_SECRET` | ✅ | 32자 16진 | |
| `THREADS_LONG_LIVED_TOKEN` | ✅ | `THAA...` 150자+ | 자동 갱신 대상 |
| `CLAUDE_AI_KEY` | 권장 | `sk-ant-...` | 없으면 정적 텍스트 |
| `GH_PAT_SECRETS_WRITE` | 권장 | `github_pat_...` | Secrets: Read and write, 레포 1개 한정 |
| `TELEGRAM_BOT_TOKEN` | 권장 | | 실패 알림 |
| `TELEGRAM_ALERT_CHAT_ID` | 권장 | | 공개 채널 ID 미사용 |
| `THREADS_USER_ID` | 선택 | 17자리 숫자 | **미설정 권장** (런타임 자동 조회) |

### Variables

| 이름 | 기본값 | 용도 |
|---|---|---|
| `YOUTUBE_URL` | — | 유입 목적지 |
| `X_URL` | — | 유입 목적지 |
| `DRY_RUN` | `true` | 발행 차단 스위치 |
| `AI_ENABLED` | `true` | AI 생성 스위치 |
| `IMAGE_FALLBACK_TO_TEXT` | `true` | 이미지 실패 시 텍스트 전용 발행 |
| `REPLY_ENABLED` | `true` | 답글 스위치 |
| `CLAUDE_MODEL` | `claude-sonnet-5` | 모델 |
| `PUBLISH_WEEKLY_REST_DAYS` | `0` | 주간 휴식일 수. 안티봇 강화 시 `1` |
| `TOKEN_ISSUED_AT` | — | 토큰 최초 발급일 `YYYY-MM-DD`. 만료 사전 경보용 |
| (워크플로우) `fetch-depth: 50` | — | 커밋 로그 수집용. 줄이면 근거 주입 실패 |
| `ASSET_RAW_BASE_URL` | **미설정 권장** | 설정 시 자동 조립을 덮어씀 |

---

## 10. 변경 절차

```
1. 로컬에서 수정
2. ruff check          → All checks passed
3. pytest              → 2회 연속 PASS
4. 커밋·푸시
5. dry_run 실행 → 로그 확인
6. live 실행
7. Threads 앱에서 실제 게시물 육안 확인
```

3번 2회 연속은 기존 배포 원칙입니다. 부분 diff보다 전체 파일 교체가 안전합니다.

---

## 부록. 미해결 항목

| 항목 | 상태 |
|---|---|
| 답글 scope 확인 | `threads_read_replies` / `threads_manage_replies` 포함 여부 미검증 |
| 프로필 automated 레이블 | 코드로 불가. 수동 기재 필요 |
| 발행 시각 최적화 | 답글 반응 데이터 축적 후 조정 |
| 성과 측정 | `threads_manage_insights` scope 미도입 |
| 이미지-본문 매칭 | 현재 날짜 기반 순환. 내용 연동 안 됨 |
