# threads-promo

Threads 무상태(stateless) 자동 발행 파이프라인.
이미지 + 짧은 글을 발행하고, YouTube / X 링크를 셀프 리플라이로 붙여 유입을 만든다.

> **v1.6.0 계정 보호(안전) 모드** — 기본값으로 모든 쓰기 자동화가 꺼져 있다(`AUTOMATION_ENABLED=false`).
> 켜도 하루 자동 게시물 2건(정기 몫 1건 예약), 링크 셀프 리플라이 0%, 답글 일 10건이 기본이다.
> 배경·운영 절차·롤백은 [DESIGN_V16_SAFETY.md](DESIGN_V16_SAFETY.md).

- DB 없음. 상태는 GitHub Secret 1개(`THREADS_LONG_LIVED_TOKEN`)뿐.
- 비용 0원. Threads API 무료, Actions 는 Public 레포 표준 러너 무료(Private 전환 시 재검토), 이미지는 레포 raw URL.

---

## 1. 설계 요약

| 항목 | 방식 |
|---|---|
| 발행 쿼터 | `threads_publishing_limit` API 조회 (DB 미사용) |
| 콘텐츠 로테이션 | `day_of_year % N` — 날짜 결정론, 멱등 |
| 홍보 : 관찰 비율 | 1 : 3 (`PROMO_CYCLE = 4`) |
| 링크 배치 | 본문 아닌 **셀프 리플라이** |
| 이미지 | 레포 `assets/` raw URL |
| 토큰 갱신 | 매 실행 시 프로그램 내 갱신 → 변수 사용 → Secret 덮어쓰기 |

---

## 2. 최초 1회 준비

### 2-1. Meta 앱

1. Meta 앱 생성 → Threads use case 추가
2. Tech Provider Verification
3. 권한 신청: `threads_basic`, `threads_content_publish`
   (답글 자동화 계획이 있으면 `threads_manage_replies`도 **지금** 함께)
4. 개발 모드에서는 발행 대상 계정을 Threads Tester로 등록·수락해야 동작

### 2-2. 토큰 발급

```bash
export THREADS_APP_ID=...
export THREADS_APP_SECRET=...
python scripts/bootstrap_token.py --code <CODE> --redirect-uri <URI>
```

인가 URL은 `scripts/bootstrap_token.py` 상단 주석 참조.
**여기서 로그인한 Threads 계정이 게시물 작성자**가 된다.

### 2-3. `config.py` 수정

`YOUTUBE_URL`이 플레이스홀더(`@REPLACE_ME`)면 실행이 즉시 중단된다.
실제 채널 핸들로 교체할 것.

---

## 3. Secrets / Variables

| 키 | 종류 | 필수 | 비고 |
|---|---|---|---|
| `THREADS_APP_ID` | Secret | ✅ | |
| `THREADS_APP_SECRET` | Secret | ✅ | |
| `THREADS_USER_ID` | Secret | ✅ | bootstrap 출력값 |
| `THREADS_LONG_LIVED_TOKEN` | Secret | ✅ | **프로그램이 매 실행 덮어씀** |
| `GH_PAT_SECRETS_WRITE` | Secret | ⚠️ | Fine-grained PAT, `secrets: write`. 없으면 60일 후 정지 |
| `TELEGRAM_BOT_TOKEN` | Secret | 권장 | 실패 알림 |
| `TELEGRAM_ALERT_CHAT_ID` | Secret | 권장 | 공개 채널 ID 사용 금지 |
| `ASSET_RAW_BASE_URL` | Variable | 선택 | 미설정 시 레포 raw URL 자동 조립 |
| `DRY_RUN` | Variable | 선택 | 기본 `true`. 실발행은 `false` |
| `AUTOMATION_ENABLED` | Variable | **v1.6.0** | 전역 킬 스위치. 기본 `false` = 정기·CHAT·STORY·답글·이어쓰기 전부 Threads 쓰기·Claude 호출 없이 종료(종료코드 0). 읽기 전용 워크플로는 동작 |
| `DAILY_POST_BUDGET` | Variable | v1.6.0 | KST 하루 자동 최상위 게시물 총량(정기+CHAT+STORY). 기본 `2`. 당첨 정기 슬롯 + 47분 전까지 정기 몫 1건 예약 |
| `LINK_REPLY_PCT` | Variable | v1.6.0 | 링크 셀프 리플라이를 다는 정기·STORY 글 비율(0~100, 게시물 ID 해시). 기본 `0` = 달지 않음 |
| `WARMUP_UNTIL` | Variable | v1.6.0 | 워밍업 종료일(KST `YYYY-MM-DD`, 그날 포함). 그때까지 정기 1건/일만, 링크·답글·이어쓰기·CHAT·STORY 중지. 형식 오류는 워밍업으로 적용 |
| `REPLY_CANNED_ENABLED` | Variable | v1.6.0 | 외국어 댓글 정형 문구. 기본 `false` = 외국어 댓글은 건너뜀 |
| `CHAT_ENABLED` | Variable | 선택 | 시장 잡담(CHAT). 기본 `false` |
| `CHAT_DAILY_MIN` / `CHAT_DAILY_MAX` | Variable | 선택 | 평일 목표 건수. 기본 5 / 15 (v1.4.0, 트리거 15개 이하로 자동 보정) |
| `CHAT_SOURCE_MODE` | Variable | 선택 | `mix`(기본) / `rss` / `web` / `none` |
| `MOOD_RSS_URLS` | Variable | 선택 | 뉴스 RSS URL, 여러 개는 `\|` 구분. 비면 웹 검색만 사용 |
| `MOOD_WEB_MAX_USES` | Variable | 선택 | CHAT 1건당 웹 검색 최대 횟수. 기본 2 |
| `REPLY_PER_RUN_CAP` | Variable | 선택 | CHAT 실행 중 답글 스윕 상한. 기본 2 (v1.6.0, 이전 4) |
| `REPLY_SCHEDULED_RUN_CAP` | Variable | 선택 | reply.yml 실행당 답글 상한. 기본 3 (v1.6.0, 이전 6) |
| `CHAT_WEEKEND_MIN` / `CHAT_WEEKEND_MAX` | Variable | 선택 | 토·일 CHAT 목표. 기본 2 / 3, 0/0 이면 주말 미발행 (v1.2.0) |
| `PILLAR_ROTATION_AUTO` | Variable | 선택 | 자동 조절 결과 전용 (v1.2.0). 수동 지정은 `PILLAR_ROTATION_OVERRIDE` |
| `REPLY_DAILY_CAP` | Variable | 선택 | 하루 댓글 답글 상한. 기본 10 (v1.6.0, 이전 40) |
| `REPLY_AUTHOR_DAILY_CAP` | Variable | 선택 | 같은 사람 하루 답글 상한. 기본 1 (v1.6.0, 이전 3) |
| `REPLY_THREAD_AUTHOR_CAP` | Variable | 선택 | 한 스레드·같은 사람 누적 상한. 기본 2 (v1.6.0, 이전 4) |
| `FOLLOWUP_ENABLED` | Variable | 선택 | 셀프 이어쓰기(내 글에 2~12시간 뒤 한 마디). 기본 `false` (v1.3.0). v1.6.0: 킬 스위치·워밍업에도 막힘 |
| `FOLLOWUP_PCT` / `FOLLOWUP_DAILY_CAP` | Variable | 선택 | 이어쓰기 대상 비율 % / 하루 상한. 기본 25 / 3 (v1.3.0) |

> 기본 `GITHUB_TOKEN`으로는 Secret 쓰기가 **불가**하다. PAT가 반드시 별도로 필요하다.

---

## 4. 실행

```bash
# 로컬 검증
DRY_RUN=true python -m src.main

# 수동 실발행
# Actions → Threads Publish → Run workflow → mode: live
```

스케줄(KST)
- 정기 발행: 08:23 / 12:47 / 20:31 중 1개
- 시장 잡담(CHAT): KST 09:00~24:00 창의 CHAT 구역 5곳(정기·이벤트 판정 창 제외)에 트리거 15개(09:04~22:54), 평일 하루 5~15건·주말 2~3건 (`chat.yml`, v1.5.0)
- 답글: 12:19 / 16:53 / 22:07 전부 + CHAT 실행마다 스윕

---

## 5. 운영 주의

- **갱신 창**: 장수명 토큰은 발급 후 24시간 경과 ~ 60일 이내에만 갱신 가능하다.
  일 1회 실행이면 항상 조건을 만족한다. **60일 이상 미실행 시 재인가 필요.**
- **갱신 실패 폴백**: 갱신에 실패해도 기존 토큰으로 발행을 계속한다.
  갱신 실패가 발행 중단으로 이어지지 않도록 의도한 설계다.
- **code 190**: 재인가가 필요한 신호다. `bootstrap_token.py`부터 다시 수행한다.
  v1.6.0: code 200(차단)·190/401(토큰 무효)은 회로 차단 — 그 실행의 이후 쓰기 0건, 재시도·텍스트 폴백 없음, 텔레그램 1회, 종료코드 7.
- **콘텐츠 린트**: 투자조언성 표현·인게이지먼트 베이트 표현이 검출되면 발행하지 않는다.
  텍스트 풀을 수정할 때 `config.FORBIDDEN_*` 목록을 함께 확인할 것.
- **시장 수치 금지**: 데이터 소스가 연결되어 있지 않으므로 텍스트 풀에 구체적 수치를
  넣지 않는다. 수치가 필요하면 데이터 파이프라인을 먼저 연결한다.
