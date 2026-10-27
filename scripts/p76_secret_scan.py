"""
scripts/p76_secret_scan.py
=======================
레포 파일에서 토큰 형식 문자열을 찾는다 (SEC T-SEC-07). 발견 시 종료코드 1.
  Meta 토큰 EAA… · 노션 ntn_/secret_ · 텔레그램 봇 토큰 · 노션 ID(32자리 16진수)
테스트용 가짜 값은 FAKE 표식이 같은 줄에 있으면 허용한다.

사용: python scripts/p76_secret_scan.py [경로…]
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "meta_token": re.compile(r"\bEAA[A-Za-z0-9]{30,}"),
    "notion_token": re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9]{30,}"),
    "telegram_bot": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "notion_id": re.compile(r"\b[0-9a-f]{32}\b"),
}
ALLOW_MARK = "FAKE"
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".pyc"}
# requirements.txt 의 sha256 해시는 64자리라 notion_id(32자리, 단어 경계)에 걸리지 않는다


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
        return [Path(p) for p in out.splitlines() if p]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in Path(".").rglob("*") if p.is_file() and ".git" not in p.parts]


def scan(paths: list[Path]) -> list[str]:
    hits = []
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(lines, 1):
            if ALLOW_MARK in line:
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    hits.append(f"{path}:{no}: {name}")
    return hits


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] if argv else tracked_files()
    hits = scan(paths)
    for hit in hits:
        print(hit)  # 위치와 종류만 출력 (값은 출력하지 않음)
    print(f"secret scan: {len(hits)} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
