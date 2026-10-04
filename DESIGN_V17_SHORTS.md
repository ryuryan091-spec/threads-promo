# DESIGN v1.7.0 — 60초 숏폼 (Facebook 페이지 릴스 · Threads 동영상)

| 항목 | 내용 |
|---|---|
| 버전 | config 1.7.0 · safety 1.1.0 · threads_client 1.4.0 · notifier 1.2.0 · redact 1.2.0 · verify_repo 1.7.0 · 신규 모듈 1.0.0 |
| 워크플로 | `.github/workflows/shorts.yml` (build → publish, publish 는 environment `meta-publish` 승인 필수) |
| 테스트 | `tests/test_shorts_v17.py` (85건, 실제 ffmpeg 렌더 E2E · 기존 파이프라인 상호작용 포함) |
| 선행 | P0 — v1.6.0 이 `src/src` · `tests/tests` · `scripts/scripts` 에 들어가 있던 배포 위치 결함 수정(이 패키지에 반영). verify_repo 검사 12 로 재발 방지 |

## 1. 원칙
- 탐지 회피·자동화 위장 기능은 만들지 않는다. 스팸처럼 보이는 행동 자체를 줄인다(상한·포맷 다양화·링크 반복 없음·사람 승인·AI 고지).
- Facebook 전용 값은 `FACE_` 접두(마스터 지시). 영상 공통 값은 `SHORTS_` 접두.
- 기본값은 전부 꺼짐. DB 없음 · 무상태 · 멱등.

## 2. 흐름
```
shorts.yml (KST 08:19)
 [build]  SHORTS_BUILD_ENABLED · 휴식일 · 램프 → 오늘 N편 계획
          mood_source(테마·분위기) → 대본(Claude) → 이미지 5장(Gemini) → TTS(Gemini) → ffmpeg → ffprobe 검사
          manifest.json + mp4 → artifact(3일) · 텔레그램 미리보기
 [publish] (has_items 일 때만) environment meta-publish 승인 대기
          오늘 content_id 만(신선도) → 게시 시각(10~22시, 첫 편 5~40분 지연, 편간 120~200분, job 330분 안)
          편마다: Facebook 릴스(같은 설명 있으면 건너뜀) → 첫 편만 Threads 동영상(킬 스위치·워밍업·총량 재확인)
          결과 알림 + "앱에서 AI 정보 표시" 안내
```

## 3. 빈도 (램프, 판단값 — Meta 기준 비공개)
| FACE_RAMP_START 부터 | Facebook/일 |
|---|---|
| 0~13일 | 1 |
| 14~41일 | 2 |
| 42일~ | 3 (FACE_DAILY_MAX 로 상한) |
Threads 는 하루 최대 1편, DAILY_POST_BUDGET(계정 합산) 안에서만. 숏폼은 비정기 글로 분류(정기 몫 예약 존중).

## 4. 포맷
F1 EDT 시장 서사(항상 첫 편) · F2 개념 해설 · F3 이번 주 관전 포인트. 같은 날 훅 유형·캡션 중복 금지.
빌런 대응(설계 결정): 금리·국채·채권 테마 → 뎁트타이탄, 긴장·경계 → 카오스리퍼, 낙관·안도 → 불브루트, 그 밖 → 뎁트타이탄.

## 5. 영상 규격
1080×1920 · 30fps · H.264 yuv420p · AAC 128k **48kHz** 스테레오 · 55~60초 · faststart · loudnorm -14 LUFS.
9비트(훅 1 + 본문 7 + 정리 1) + 아웃트로 2초. 낭독 속도 상한 1.25배, 넘으면 그 편 실패.
연출 값(훅 펀치인·흔들림·노랑 자막·Ken Burns·아웃트로 로고)은 investment_comic_tube 렌더러에서 확인한 값.

## 6. 규제·정책
- 대사·자막·캡션: `content.lint_shorts` = CHAT 규칙(숫자 금지 REG-03, 기업·인물 금지 REG-04, 투자조언·베이트·링크·해시태그 금지, 영문 허용목록) + `EDT` 허용.
- 캡션 끝 AI 고지 고정 문구 + 앱에서 AI 정보 수동 표시(공식 API 파라미터 미확인).

## 7. 변수
| 이름 | 종류 | 기본 |
|---|---|---|
| SHORTS_BUILD_ENABLED | Variable | false |
| SHORTS_THREADS_ENABLED | Variable | false |
| FACE_ENABLED | Variable | false |
| FACE_RAMP_START | Variable | (없음 = 0편) |
| FACE_DAILY_MAX | Variable | 3 |
| SHORTS_WEEKLY_REST_DAYS | Variable | 1 |
| SHORTS_IMAGE_MODEL / SHORTS_TTS_MODEL / SHORTS_TTS_VOICE | Variable | gemini-3.1-flash-image / gemini-3.1-flash-tts-preview / Charon |
| FACE_PAGE_ID · FACE_PAGE_TOKEN | Secret | — |
| GEMINI_API_SUB_PAY_KEY | Secret | — |
| CLAUDE_AI_KEY | Secret | 기존 |

## 8. 종료코드
build: 0 정상 · 2 전부 실패 · 3 Secret 누락 / publish: 0 · 4 일부 실패 · 7 계정·토큰 사용 불가(회로 차단)

## 9. 기존 Threads 파이프라인 영향 차단 (v1.7.0 전수테스트에서 발견·수정)
Threads 동영상(media_type=VIDEO)이 계정에 섞이면 '비CHAT = 정기·이벤트'로 보던 판정이 영상을 정기 글로 오인한다.
SHORTS_THREADS_ENABLED=true 일 때만 생기는 문제이며 아래처럼 VIDEO 를 제외했다(`chat_plan.is_shorts_post`).

| 위치 | 수정 전 영향 | 수정 |
|---|---|---|
| run_watchdog 정기 신선도 | 정기 발행이 멈춰도 영상이 경보를 가림 | VIDEO 제외 |
| run_story 간격·상한 | 영상 뒤 4시간 STORY 차단, 이벤트 상한 소모 | VIDEO 제외 |
| safety.regular_reserved | 정기 창 안 영상을 정기 완료로 보고 예약 해제 → CHAT 이 정기 몫 사용 | VIDEO 제외 |
| insights.restore_pillar | 정기 창 안 영상을 그 슬롯 기둥으로 귀속 | `SHORTS` 로 분리 |
| insights 리포트 / run_weighting | 영상 지표가 기둥 순위·가중치에 섞임 | SHORTS 는 표시만, 순위·가중치 제외 |
| 일일 총량(DAILY_POST_BUDGET) | 영상도 계정 게시물 | **그대로 합산(의도)** |

차등 검증: v1.6 트리와 같은 입력으로 린트 3,137건×3, CHAT 판정·기둥 복원 2,472건, 예산·예약 3,000건, 마스킹·컨테이너 대기·알림 결과가 전부 동일(VIDEO 미포함 입력). 기존 테스트 1,155건은 파일 변경 없이 그대로 통과.

## 10. 미결
| # | 항목 |
|---|---|
| V1 | 페이지 토큰으로 릴스 3단계 실게시 확인 |
| V2 | 이미지 품질(1K 생성 → 1080×1920 크롭) |
| V3 | raw.githubusercontent URL 을 Threads 가 VIDEO 로 처리하는지(Content-Type 로그 확인) |
| V4 | TTS 실제 낭독 속도 → 9비트 300~400자 대본이 55~60초에 들어가는지 |
| C1 | 카오스리퍼·불브루트 외형 묘사는 임시. investment_comic_tube characters.py 원문 확보 후 교체 |
| C2 | assets/video/* 는 비어 있음 — YouTube 레포 assets 복사 필요(없으면 BGM·효과음·로고 없이 동작) |
| C3 | 숫자 기반 훅 불가(REG-03). 수치 훅이 필요하면 별도 결정 |
