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
"""

from __future__ import annotations

import importlib
import os
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

VERSION = "1.0.0"

REQUIRED_MODULES = [
    "config", "env", "ai_writer", "antibot", "content", "facts",
    "main", "notifier", "notion_source", "reply_engine",
    "run_reply", "run_story", "run_watchdog", "threads_client",
    "token_manager", "watchdog",
]

REQUIRED_FILES = [
    "requirements.txt",
    "pyproject.toml",
    ".github/workflows/publish.yml",
    ".github/workflows/reply.yml",
    ".github/workflows/watchdog.yml",
    ".github/workflows/story.yml",
    ".github/workflows/verify_token.yml",
]

# 워크플로우가 실행하는 모듈
WORKFLOW_ENTRYPOINTS = {
    ".github/workflows/publish.yml": "src.main",
    ".github/workflows/reply.yml": "src.run_reply",
    ".github/workflows/watchdog.yml": "src.run_watchdog",
    ".github/workflows/story.yml": "src.run_story",
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


def main() -> int:
    print(f"[VerifyRepo] v{VERSION}")
    print(f"경로: {REPO_ROOT}")

    total = 0
    total += check_files()
    total += check_modules()
    total += check_workflows()
    total += check_entrypoints()
    total += check_slot_mapping()

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
