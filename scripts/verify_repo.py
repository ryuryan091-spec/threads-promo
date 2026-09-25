"""레포 무결성 검증.

웹 UI 부분 업로드가 실패하면 일부 파일만 반영된 상태가 남는다.
그러면 워크플로우는 존재하는데 참조 모듈이 없어 런타임에 깨진다.
이 스크립트는 그런 부분 반영 상태를 찾아낸다.

사용
  python scripts/verify_repo.py

검사
  1. 필수 파일 존재 여부
  2. 모든 파이썬 모듈 임포트 가능 여부
  3. 모든 워크플로우 YAML 파싱 가능 여부
  4. 워크플로우가 참조하는 모듈이 실제로 있는지
  5. cron 과 slot 매핑 일치 여부
  6. chat.yml cron 과 config.CHAT_TRIGGERS(KST) 일치 여부
"""

from __future__ import annotations

import importlib
import os
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

VERSION = "1.3.0"   # v1.3.0: DRY_RUN 식·슬롯 목록·정기/이벤트 cron 대조 검사

REQUIRED_MODULES = [
    "config", "env", "ai_writer", "antibot", "chat_plan", "content", "facts",
    "main", "mood_source", "notifier", "notion_source", "redact", "reply_engine",
    "insights", "run_chat", "run_insights", "run_refresh", "run_reply", "run_story",
    "run_watchdog", "run_weighting", "threads_client", "weighting",
    "token_manager", "watchdog",
]

REQUIRED_FILES = [
    "requirements.txt",
    "pyproject.toml",
    ".github/workflows/publish.yml",
    ".github/workflows/reply.yml",
    ".github/workflows/chat.yml",
    ".github/workflows/golive_check.yml",
    ".github/workflows/watchdog.yml",
    ".github/workflows/story.yml",
    ".github/workflows/token_refresh.yml",
    ".github/workflows/insights.yml",
    ".github/workflows/weighting.yml",
    ".github/workflows/verify_token.yml",
]

# 워크플로우가 실행하는 모듈
WORKFLOW_ENTRYPOINTS = {
    ".github/workflows/publish.yml": "src.main",
    ".github/workflows/reply.yml": "src.run_reply",
    ".github/workflows/chat.yml": "src.run_chat",
    ".github/workflows/watchdog.yml": "src.run_watchdog",
    ".github/workflows/story.yml": "src.run_story",
    ".github/workflows/token_refresh.yml": "src.run_refresh",
    ".github/workflows/insights.yml": "src.run_insights",
    ".github/workflows/weighting.yml": "src.run_weighting",
}


def _ok(msg: str) -> None:
    print(f"  [OK  ] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check_files() -> int:
    print("\n1. 필수 파일")
    missing = 0
    for rel in REQUIRED_FILES:
        path = REPO_ROOT / rel
        if path.exists():
            _ok(f"{rel} ({path.stat().st_size} bytes)")
        else:
            _fail(f"{rel} 없음")
            missing += 1
    return missing


def check_modules() -> int:
    print("\n2. 모듈 임포트")
    # 링크 검증에 걸리지 않도록 임시값 주입
    os.environ.setdefault("YOUTUBE_URL", "https://example.com")
    os.environ.setdefault("X_URL", "https://example.com")

    failed = 0
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(f"src.{name}")
        except Exception as exc:  # noqa: BLE001
            _fail(f"src/{name}.py — {type(exc).__name__}: {exc}")
            failed += 1
    if not failed:
        _ok(f"{len(REQUIRED_MODULES)}개 모듈 전부 정상")
    return failed


def check_workflows() -> int:
    print("\n3. 워크플로우 YAML")
    try:
        import yaml
    except ImportError:
        print("  [SKIP] pyyaml 미설치 — pip install pyyaml 후 재실행")
        return 0

    failed = 0
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _fail(f"{path.name} — 파싱 실패: {exc}")
            failed += 1
            continue

        if not isinstance(data, dict) or "jobs" not in data:
            _fail(f"{path.name} — jobs 섹션 없음")
            failed += 1
            continue

        _ok(f"{path.name} — {data.get('name', '(이름없음)')}")
    return failed


def check_entrypoints() -> int:
    print("\n4. 워크플로우 진입점")
    failed = 0
    for rel, module in WORKFLOW_ENTRYPOINTS.items():
        path = REPO_ROOT / rel
        if not path.exists():
            _fail(f"{rel} 없음")
            failed += 1
            continue

        body = path.read_text(encoding="utf-8")
        if f"python -m {module}" not in body:
            _fail(f"{rel} — '{module}' 실행 구문 없음")
            failed += 1
            continue

        target = REPO_ROOT / (module.replace(".", "/") + ".py")
        if not target.exists():
            _fail(f"{rel} → {target.name} 파일이 없습니다 (부분 반영 의심)")
            failed += 1
            continue

        _ok(f"{rel} → {module}")
    return failed


def check_slot_mapping() -> int:
    print("\n5. cron 과 slot 매핑")
    try:
        import yaml
    except ImportError:
        print("  [SKIP] pyyaml 미설치")
        return 0

    failed = 0
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        body = path.read_text(encoding="utf-8")
        data = yaml.safe_load(body) or {}
        on = data.get(True) or data.get("on") or {}
        crons = [c["cron"] for c in (on.get("schedule") or [])]
        cases = re.findall(r'"([\d\* ]+)"\)\s*echo "slot=', body)

        if not crons or not cases:
            continue

        if sorted(crons) != sorted(cases):
            _fail(
                f"{path.name} — cron {sorted(crons)} 과 "
                f"slot 분기 {sorted(cases)} 불일치. "
                "슬롯이 MANUAL 로 떨어져 매번 실행됩니다."
            )
            failed += 1
        else:
            _ok(f"{path.name} — cron {len(crons)}개 일치")
    return failed


def kst_to_utc_cron(hhmm: str) -> str:
    """'09:04' (KST) -> '4 0 * * *' (UTC). 하루를 넘기는 시각도 처리한다."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    return f"{minute} {(hour - 9) % 24} * * *"


def check_chat_triggers() -> int:
    print("\n6. chat.yml cron 과 CHAT_TRIGGERS")
    try:
        import yaml
    except ImportError:
        print("  [SKIP] pyyaml 미설치")
        return 0

    from src import config

    path = REPO_ROOT / ".github" / "workflows" / "chat.yml"
    if not path.exists():
        _fail("chat.yml 없음")
        return 1

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    on = data.get(True) or data.get("on") or {}
    crons = [c["cron"] for c in (on.get("schedule") or [])]
    expected = [kst_to_utc_cron(t) for t in config.CHAT_TRIGGERS]

    if crons != expected:
        _fail(
            f"chat.yml cron {crons} 이 CHAT_TRIGGERS 환산값 {expected} 과 다릅니다. "
            "트리거 번호(T1~)가 어긋나면 발행 계획이 틀어집니다."
        )
        return 1
    _ok(f"chat.yml — 트리거 {len(crons)}개 순서까지 일치")
    return 0


# 수동 실행은 mode 로만, 예약 실행은 vars.DRY_RUN 으로 결정한다.
# 이전 식(… && 'false' || vars.DRY_RUN || 'true')은 수동 dry_run 이 vars.DRY_RUN=false 로 떨어져
# 실제 발행되었다(2026-09-26 검토).
CANON_DRY_RUN = (
    "${{ github.event_name == 'workflow_dispatch' && "
    "(inputs.mode == 'live' && 'false' || 'true') || (vars.DRY_RUN || 'true') }}"
)
DRY_RUN_WORKFLOWS = ("publish.yml", "reply.yml", "chat.yml", "story.yml")


def _load_yaml(name: str) -> dict:
    import yaml

    path = REPO_ROOT / ".github" / "workflows" / name
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _step_envs(data: dict) -> list[dict]:
    envs: list[dict] = []
    for job in (data.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            envs.append(step.get("env") or {})
    return envs


def check_dry_run_expr() -> int:
    print("\n7. DRY_RUN 식 (수동 dry_run 이 실제 발행으로 새지 않는지)")
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("  [SKIP] pyyaml 미설치")
        return 0

    failed = 0
    for name in DRY_RUN_WORKFLOWS:
        data = _load_yaml(name)
        on = data.get(True) or data.get("on") or {}
        mode = ((on.get("workflow_dispatch") or {}).get("inputs") or {}).get("mode") or {}
        if set(mode.get("options") or []) != {"dry_run", "live"}:
            _fail(f"{name} — inputs.mode 선택지가 dry_run/live 가 아닙니다")
            failed += 1
            continue
        values = [e["DRY_RUN"] for e in _step_envs(data) if "DRY_RUN" in e]
        if not values or any(str(v).strip() != CANON_DRY_RUN for v in values):
            _fail(f"{name} — DRY_RUN 식이 표준과 다릅니다: {values}")
            failed += 1
            continue
        _ok(f"{name} — 표준 식 (수동 기본값 {mode.get('default')})")
    return failed


def _utc_cron_to_kst(cron: str) -> str:
    minute, hour = cron.split()[:2]
    return f"{(int(hour) + 9) % 24:02d}:{int(minute):02d}"


def check_slot_constants() -> int:
    print("\n8. 정기·이벤트 cron 과 코드 상수")
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("  [SKIP] pyyaml 미설치")
        return 0

    from src import config, insights

    failed = 0
    publish = _load_yaml("publish.yml")
    story = _load_yaml("story.yml")

    for name, data in (("publish.yml", publish), ("story.yml", story)):
        slots = [e.get("PUBLISH_SLOTS") for e in _step_envs(data) if "PUBLISH_SLOTS" in e]
        expected = ",".join(config.PUBLISH_SLOTS)
        if slots != [expected]:
            _fail(f"{name} — PUBLISH_SLOTS {slots} 이 config {expected!r} 와 다릅니다")
            failed += 1
        else:
            _ok(f"{name} — PUBLISH_SLOTS {expected}")

    def _crons(data: dict) -> list[str]:
        on = data.get(True) or data.get("on") or {}
        return [c["cron"] for c in (on.get("schedule") or [])]

    pub_kst = sorted(_utc_cron_to_kst(c) for c in _crons(publish))
    if pub_kst != sorted(insights.PUBLISH_SLOTS):
        _fail(f"publish.yml cron(KST) {pub_kst} ≠ insights.PUBLISH_SLOTS {sorted(insights.PUBLISH_SLOTS)}"
              " — 기둥 복원이 조용히 틀어집니다")
        failed += 1
    else:
        _ok(f"publish.yml cron ↔ insights.PUBLISH_SLOTS 일치 {pub_kst}")

    ev_kst = sorted(_utc_cron_to_kst(c) for c in _crons(story))
    if ev_kst != sorted(insights.EVENT_SLOTS):
        _fail(f"story.yml cron(KST) {ev_kst} ≠ insights.EVENT_SLOTS {sorted(insights.EVENT_SLOTS)}")
        failed += 1
    else:
        _ok(f"story.yml cron ↔ insights.EVENT_SLOTS 일치 {ev_kst}")
    return failed


CHAT_PLAN_KEYS = (
    "CHAT_ENABLED", "CHAT_DAILY_MIN", "CHAT_DAILY_MAX", "CHAT_WEEKEND_MIN", "CHAT_WEEKEND_MAX",
)
CHAT_PLAN_WORKFLOWS = ("chat.yml", "watchdog.yml", "golive_check.yml")


def check_chat_env_consistency() -> int:
    """CHAT 계획 변수는 발행(chat)·감시(watchdog)·점검(golive) 이 같은 값을 봐야 한다.

    watchdog 에 주말 목표가 빠지면 '주말 0건' 설정에서 매주 무발행 오탐이 난다.
    """
    print("\n9. CHAT 계획 변수 일치 (chat / watchdog / golive_check)")
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("  [SKIP] pyyaml 미설치")
        return 0

    failed = 0
    reference: dict[str, str] | None = None
    for name in CHAT_PLAN_WORKFLOWS:
        merged: dict[str, str] = {}
        for env in _step_envs(_load_yaml(name)):
            merged.update({k: str(v) for k, v in env.items() if k in CHAT_PLAN_KEYS})
        missing = [k for k in CHAT_PLAN_KEYS if k not in merged]
        if missing:
            _fail(f"{name} — 누락 {missing}")
            failed += 1
            continue
        if reference is None:
            reference = merged
        elif merged != reference:
            _fail(f"{name} — chat.yml 과 값이 다릅니다 {merged}")
            failed += 1
            continue
        _ok(f"{name} — CHAT 계획 변수 {len(CHAT_PLAN_KEYS)}개 일치")
    return failed


def main() -> int:
    print(f"[VerifyRepo] v{VERSION}")
    print(f"경로: {REPO_ROOT}")

    total = 0
    total += check_files()
    total += check_modules()
    total += check_workflows()
    total += check_entrypoints()
    total += check_slot_mapping()
    total += check_chat_triggers()
    total += check_dry_run_expr()
    total += check_slot_constants()
    total += check_chat_env_consistency()

    print("\n" + "=" * 52)
    if total:
        print(f"이상 {total}건. 위 [FAIL] 항목을 확인하십시오.")
        print("부분 반영 상태일 수 있습니다. 누락 파일을 마저 커밋하거나,")
        print("git 으로 전체를 한 번에 반영하십시오.")
        return 1

    print("이상 없음. 레포 구성이 완전합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
