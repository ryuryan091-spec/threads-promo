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
| `p76_insights.yml` | 매일 06:20 | 발행 후 D1·D3·D7 반응·댓글수·공유수·팔로워 → DB-08 | `+ PAGE76_INSIGHTS_ENABLED` |

캘린더는 21:45 예약 작업 전에 돌고, CHAT 트리거(21:19)와 6분 이상 떨어지도록 21:28 에 둔다.
CI 는 레포 공용 `ci.yml` 이 `p76` 린트 · 비밀 패턴 검사 · `tests/p76` 를 함께 돌린다.

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
