# threads-promo

Threads 무상태(stateless) 자동 발행 파이프라인.
이미지 + 짧은 글을 발행하고, YouTube / X 링크를 셀프 리플라이로 붙여 유입을 만든다.

- DB 없음. 상태는 GitHub Secret 1개(`THREADS_LONG_LIVED_TOKEN`)뿐.
- 비용 0원. Threads API 무료, Actions 프라이빗 월 2,000분 내, 이미지는 레포 raw URL.

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

> 기본 `GITHUB_TOKEN`으로는 Secret 쓰기가 **불가**하다. PAT가 반드시 별도로 필요하다.

---

## 4. 실행

```bash
# 로컬 검증
DRY_RUN=true python -m src.main

# 수동 실발행
# Actions → Threads Publish → Run workflow → mode: live
```

스케줄: 매일 KST 09:30 (`cron: 30 0 * * *`).

---

## 5. 운영 주의

- **갱신 창**: 장수명 토큰은 발급 후 24시간 경과 ~ 60일 이내에만 갱신 가능하다.
  일 1회 실행이면 항상 조건을 만족한다. **60일 이상 미실행 시 재인가 필요.**
- **갱신 실패 폴백**: 갱신에 실패해도 기존 토큰으로 발행을 계속한다.
  갱신 실패가 발행 중단으로 이어지지 않도록 의도한 설계다.
- **code 190**: 재인가가 필요한 신호다. `bootstrap_token.py`부터 다시 수행한다.
- **콘텐츠 린트**: 투자조언성 표현·인게이지먼트 베이트 표현이 검출되면 발행하지 않는다.
  텍스트 풀을 수정할 때 `config.FORBIDDEN_*` 목록을 함께 확인할 것.
- **시장 수치 금지**: 데이터 소스가 연결되어 있지 않으므로 텍스트 풀에 구체적 수치를
  넣지 않는다. 수치가 필요하면 데이터 파이프라인을 먼저 연결한다.
