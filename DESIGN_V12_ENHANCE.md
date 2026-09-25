# v1.2.0 고도화 — 결함 수정·측정 보정·안티봇 강화

상태 **구현·파일럿 테스트 완료 · 배포 대기** · 작성 2026-09-26
기준 코드 main `296c764` (2026-09-21) · 검토 문서 `REVIEW_2026-09-26` (프로젝트)

---

## 0. 요약

| 구분 | 항목 | 결과 |
|---|---|---|
| Phase 1 결함 | F1~F7 | 전부 수정 |
| Phase 2 측정 | M1·M2·M3·M6 | 수정. M4(Notion 적재)·M5(기둥별 링크 귀속)는 보류(아래 6장) |
| Phase 3 안티봇 | Q1~Q5 | 수정. Q6(이미지·기둥 매핑)은 보류 |
| 위생 | bootstrap 인젝션, 이벤트 감지 창, verify_repo 검사 3종 | 수정 |
| 독립 리뷰 반영 | R1~R4 | 수정 |
| 검증 | pytest 674 PASS ×2(네트워크 차단), ruff, verify_repo 48 OK, E2E 11종, 시뮬레이션 7종 | 전부 PASS |

---

## 1. 영향 파일 체크리스트

```
┌──────────────────────────────────────────────────────────────┐
│ [src]                                                         │
│ ■ config.py        v1.2.0  신규 상수 9종                       │
│ ■ ai_writer.py     v1.2.0  마무리 지시 분리, AUTO 로테이션      │
│ ■ content.py       v1.2.0  pillar 강제, closing, 리플 문구 회전 │
│ ■ chat_plan.py     v1.1.0  주말 목표, 소재 비복원, 마무리 비율   │
│ ■ run_chat.py      v1.1.0  F1·F7, 스윕 시간 예산                │
│ ■ run_reply.py     v1.2.0  F5·F6·Q5, 스윕 시간 예산             │
│ ■ reply_engine.py  v1.2.0  제3자 간 대화 스킵                   │
│ ■ threads_client.py v1.2.0 대화 페이지네이션                    │
│ ■ run_story.py     v1.2.0  F3, 가변 감지 창, 휴식일, 자정 재검증 │
│ ■ insights.py      v1.2.0  이벤트=STORY, 창 확대·자정 처리       │
│ ■ weighting.py     v1.1.0  로테이션 판정 위임                   │
│ ■ run_weighting.py v1.2.0  클릭 배분 제거, AUTO 안내            │
│ ■ run_watchdog.py  v1.2.0  링크 리플 제외, 주말 0건 반영        │
│ [scripts]                                                     │
│ ■ verify_repo.py   v1.3.0  검사 7·8·9 추가                      │
│ ■ golive_check.py  v1.1.0  AUTO 로테이션 점검                   │
│ [workflows] (별도 반영)                                        │
│ ■ publish / reply / chat / story  DRY_RUN 식                   │
│ ■ reply.yml  timeout 20→30, REPLY_SCHEDULED_RUN_CAP            │
│ ■ chat.yml / watchdog.yml / golive_check.yml  CHAT_WEEKEND_*    │
│ ■ story.yml  PUBLISH_SLOTS, PUBLISH_WEEKLY_REST_DAYS            │
│ ■ publish / story / insights / weighting / golive  AUTO 변수    │
│ ■ insights.yml  INSIGHTS_POST_LIMIT 7→70                        │
│ ■ bootstrap.yml  입력값 env 경유                                │
│ [tests]                                                       │
│ ■ test_enhance_v12.py (신규 79건) + 기존 5개 파일 기대값 갱신   │
│ [docs] OPERATIONS.md / README.md / 본 문서                      │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 결함 수정 (Phase 1)

| # | 결함 | 근거 | 수정 |
|---|---|---|---|
| F1 | CHAT_ENABLED=false 여도 발행 | 09-21 웹 편집으로 `return 0` 삭제. `test_disabled_does_nothing` 실패 | 복원 |
| F2 | 수동 mode=dry_run 이 vars.DRY_RUN=false 면 실발행 | `… && 'false' \|\| vars.DRY_RUN \|\| 'true'` | `event=='workflow_dispatch' && (mode=='live' && 'false' \|\| 'true') \|\| (vars.DRY_RUN \|\| 'true')` 4개 파일 동일. verify_repo 검사 7 |
| F3 | 이벤트 STORY 발행 가능일 0/365 | 슬롯 A~C 중 하나라도 STORY 면 차단 + 이벤트 기둥이 STORY 여야 발행 → 동시 충족 불가 | 이벤트 기둥 STORY 강제(`build_plan(pillar=)`), 차단은 당첨 슬롯 예측(`antibot.choose_slot`, 정기와 동일 솔트) + 오늘 STORY 발행 실측. 가능일 215/365 |
| F4 | 대화 조회 25건에서 잘림 → 중복 답글 | `get_conversation` 커서 미추적 | `paging.cursors.after` 추적, 25×최대 4페이지 |
| F5 | 컨테이너 대기 실패 1건에 스윕 전체 중단 | `ContainerNotReadyError` 는 ThreadsApiError 계열 아님 | 건별 실패 처리·알림 후 계속 |
| F6 | reply.yml 실행당 상한 20 → 지연만 최대 47.5분 > timeout 20분 | `per_run_cap=REPLY_DAILY_CAP` | `REPLY_SCHEDULED_RUN_CAP=6` + 스윕 시간 예산(R2) |
| F7 | 예약 CHAT + DRY_RUN 이면 게이트 보류여도 Claude 호출 | `if blocked and not dry_run` | 미리보기는 수동 dry_run 만 |

---

## 3. 측정 보정 (Phase 2)

| # | 내용 | 수정 |
|---|---|---|
| M1 | 인사이트 표본 7건 | insights.yml 기본값 70 (코드와 일치) |
| M2 | 클릭을 게시물 수 비례로 배분 → 기둥별 평균이 모두 같음 | `WEIGHT_SCORE_CLICKS=0.0`, 배분 제거, 리포트에 계정 합계만 |
| M3 | 자동 결과를 OVERRIDE 에 넣게 안내 → 이후 자동 조절 영구 정지 | `PILLAR_ROTATION_AUTO` 신설. 우선순위 OVERRIDE > AUTO > 기본. AUTO 는 불변식(S4·S5·S7·연속중복) 위반 시 무시 |
| M6 | 워치독 답글 감시가 링크 셀프 리플라이에 가려짐 | 원글 직속 내 답글(`replied_to.id == post_id`) 제외 |

---

## 4. 안티봇·콘텐츠 (Phase 3)

| # | 내용 | 수정 | 결과(1년 시뮬레이션) |
|---|---|---|---|
| Q1 | CHAT 소재 트리거별 독립 추첨 | 날짜 시드 순열 + 트리거 번호(비복원) | 소재 중복일 268 → 0 |
| Q2 | 모든 글이 질문으로 종결 | 지시문에서 "질문으로 닫는다" 제거, `# 마무리` 블록 주입. 정기 `CLOSING_PATTERN=Q,S,Q,Q,S`(60%), CHAT 55% | CHAT 질문 55.1% |
| Q3 | 링크 리플 첫 줄 매일 동일 | `REPLY_LEADS` 6종 회전(마무리 패턴 5와 서로소) | — |
| Q4 | 주말에도 평일과 같은 CHAT | `CHAT_WEEKEND_MIN/MAX` 기본 2/3, 0/0 이면 주말 미발행 | 주간 49 → 39.5건 |
| Q5 | 제3자끼리의 대화에 끼어듦 | 부모가 내 원글·내 답글일 때만 응답 | — |

---

## 5. 독립 리뷰 반영 (상위 모델 교차 검토)

| # | 지적 | 수정 |
|---|---|---|
| R1 | 이벤트 발행(지연+지터 최대 90분)이 55분 창 밖이면 STORY 로 인식 안 됨 → 같은 회차 이중 발행 가능. 23:17 이벤트가 자정을 넘기면 판정불가. 재검증이 지터 전 날짜 사용 | 이벤트 창 90분, 정기 창 35분, 분 계산 `% 1440`. 재검증 날짜 재계산. 창 겹침 전수 테스트. 이벤트 복원율 55.1% → 100% |
| R2 | 컨테이너 IN_PROGRESS 대기(최대 300초)가 겹치면 timeout 초과 | 스윕 시간 예산: 다음 건 최악 소요(지연150+대기300+5+여유40)가 남은 시간을 넘으면 중단. reply.yml timeout 30분·예산 25분, CHAT 은 잔여 시간(20분 기준) |
| R3 | 휴식일에 이벤트 STORY 발행 | 휴식일은 이벤트도 쉼 |
| R4 | watchdog.yml 에 주말 변수 없음 → 주말 0건 설정 시 매주 오탐 | watchdog·golive 에 CHAT 계획 변수 5종 추가, verify_repo 검사 9 |

---

## 6. 보류 항목 (사유)

| # | 항목 | 사유 |
|---|---|---|
| M4 | 인사이트·가중치 결과 Notion 적재 | 신규 DB·Secret 필요 — 마스터 결정 사항 |
| M5 | 링크 파라미터로 기둥별 클릭 귀속 | `link_total_values` 가 URL별로 오는지, 쿼리 파라미터가 보존되는지 실측 필요 |
| Q6 | 이미지·기둥 매핑 | 자산 20개에 기둥 메타데이터 없음 — 자산 분류 필요 |
| — | statement 마무리 강제 린트 | 강제 시 재생성 소진 → 정적 폴백(질문 종결) 증가. 로그로 실측 후 판단 |

---

## 7. 파일럿 테스트 결과

| # | 항목 | 결과 |
|---|---|---|
| T1 | ruff / compileall / YAML 12 / verify_repo | PASS (OK 48, 신규 검사 7·8·9 포함) |
| T2 | pytest ×2 (네트워크 소켓 차단) | 674 passed ×2 |
| T3 | 커버리지 | 전체 88% · chat_plan 99 · run_chat 98 · ai_writer 97 · insights 97 · run_watchdog 97 · run_reply 92 |
| T4 | 원본 테스트 593건 × 신규 코드 | 16건 실패 — 전부 의도한 명세 변경(함수명 교체·기대값 변경). 해당 테스트는 갱신 |
| T5 | verify_repo 음성 검사 | 이전 DRY_RUN 식 되돌림 → FAIL 검출 |
| T6 | E2E 11종 (faketime + HTTP 계층 가짜, 가상 시계) | 전부 기대 동작, 자격증명 노출 0 |
| T7 | 시뮬레이션 | 아래 표 |

E2E 시나리오

| 시나리오 | 시각(KST) | 결과 |
|---|---|---|
| chat_live | 09-28(월) 10:07 T4 | 텍스트 1건 발행, 웹검색 1·생성 1, 마무리=question, 소재 비복원 배정 |
| chat_disabled | 10:07 | Threads 호출 0, Claude 0 |
| chat_sched_dryrun_blocked | 12:20 T9 | 창 밖 보류, Claude 0 |
| chat_weekend_zero | 09-26(토) | 목표 0건 보류 |
| reply_sched | 12:19 | 30건 대화(2페이지). 26번째 이후 내 답글 인식(중복 0), 제3자 대화 스킵, 컨테이너 ERROR 1건 후 계속, 상한 6 |
| reply_inprogress | 12:19 | 전 컨테이너 IN_PROGRESS → 3건 실패 후 예산 중단, 가상 경과 1,138초 < 예산 1,500초 |
| story_ok | 09-28 13:29 | 감지 창 12.36h, 기둥 STORY 강제, 이미지+링크 리플 발행 |
| story_block | 09-29 13:29 | 당첨 슬롯 기둥 STORY → 보류 |
| weighting | 09-27 05:37 | 리포트에 계정 클릭 합계, 점수 제외 안내 |
| publish | 09-28 12:47 | 정기 발행, 리플 첫 줄 회전, 마무리 블록 주입 |
| watchdog | 09-28 21:37 | 링크 리플 제외 확인 |

시뮬레이션 (이전 → 이후)

| 항목 | 이전 | 이후 |
|---|---|---|
| 이벤트 STORY 가능일(365) | 0 | 215 |
| CHAT 소재 중복일(365) | 268 | 0 |
| 이벤트 감지 누락(7일, 분) | 1,259 | 0 |
| 이벤트 글 STORY 복원율 | 55.1% | 100% |
| reply.yml 최악 지연 합계 | 2,850초 (timeout 1,200) | 750초 + 예산 가드 (timeout 1,800) |
| 주간 CHAT 목표 | 49 | 39.5 (주말 2~3) |

---

## 7-2. 전수 통합테스트 (배포본 기준, 2026-09-26)

대상: 원격 main `296c764` 새 clone + v1.2.0 ZIP 2개 적용본 (작업본과 파일 단위 동일 확인)

| # | 항목 | 결과 |
|---|---|---|
| T1 | ruff / compileall / YAML 12 / verify_repo | PASS (OK 48, FAIL 0) |
| T2 | pytest ×2 (네트워크 소켓 차단) | 674 passed ×2 |
| T3 | 커버리지 | 전체 88% |
| T4 | 워크플로 env 감사 | v1.2.0 신규·연관 키 누락 0, 숫자 파싱 키 15개 전부 yml 기본값(`\|\|`) 보유, permissions 전부 contents: read |
| T5 | cron·동시성 감사 | 24개, 정각/반각 0, 5분 내 근접 0, CHAT 창 안 타 워크플로는 watchdog 09:53(읽기 전용)뿐, 그룹 내 timeout 겹침 0 |
| T6 | 보안 | 하드코딩 자격증명 0(문서 자리표시자 1건 제외), 실제 네트워크 오류 경로 `access_token=***` 마스킹, run: 직접 삽입 입력은 boolean 1건뿐 |
| T7 | 시각 행렬 (faketime) | 60개 시각(KST 매시 :17·:47 + 창 경계·자정·월말·연말·2월말·주말) 전부 PASS |
| T8 | 원본 테스트 593건 × 신규 코드 | 16건 실패 — 전부 의도한 명세 변경(이전 보고와 동일 목록) |
| T9 | E2E 15종 | 전부 기대 동작, 자격증명 노출 0 |
| T10 | CHAT 운영 시뮬레이션 (실제 gate, cron 누락·지연·직렬화) | 정상 100% · 누락5% 98.0% · 누락17% 86.9% · 혼잡30% 61.4% · 1년 84.0%, 간격 위반 0, 창 밖 발행 0, 목표 초과 0. 주말 일평균 2.1~2.7건 |
| T11 | Go-Live Check (가짜 자격증명·네트워크 차단) | C2 로테이션 AUTO 적용 확인, C4·C9 FAIL → 종료코드 1(설계대로), 출력 자격증명 0 |
| T12 | ZIP 재추출 | 작업본과 동일 |

발견·수정 1건
- T7 에서 `test_story_event::TestPostJitterRecheck` 가 실행 시각 20:xx·23:xx 에 실패. 픽스처 `_post_now(20)` 이 확장된 이벤트 판정 창(03:11·23:17 +90분)에 들어가 'STORY 발행됨'으로 차단된 것. 코드 결함이 아니라 테스트의 시각 의존 → 재검증 동작만 보도록 `_story_published_today` 고정.

관찰(조치 불요)
- `config.py` 의 `int(os.environ.get(...))` 는 빈 문자열 주입 시 import 에서 실패한다(v1.1 이전부터 동일 패턴). 현재 모든 워크플로가 `|| '기본값'` 으로 전달해 운영 경로 위험은 0. 신규 숫자 변수 추가 시 yml 기본값 필수.

## 8. 운영 반영 절차

1. 코드 ZIP 반영 → 워크플로 파일 반영(별도)
2. Actions → **Threads Go-Live Check** 수동 실행 (적용 로테이션·AUTO 확인)
3. **Threads Chat** 수동 `dry_run` 1회 — 로그 `마무리=`·`소재=` 확인
4. **Threads Reply** 수동 `dry_run` 1회 — `제3자 간 대화` 스킵 로그 확인
5. Variables (선택): `CHAT_WEEKEND_MIN/MAX`, `REPLY_SCHEDULED_RUN_CAP`. 과거 자동 결과를 `PILLAR_ROTATION_OVERRIDE` 에 넣어 두었다면 `PILLAR_ROTATION_AUTO` 로 이관

## 9. 변경 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 1.0 | 2026-09-26 | 최초 작성 — 검토·상세설계·구현·파일럿·독립 리뷰 반영 |
