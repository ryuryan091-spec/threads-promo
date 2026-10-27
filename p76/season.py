"""
p76/season.py
=============
일지번호 → 시즌·위치 (예약 작업 프롬프트 v2.3 STEP 1-8 과 같은 규칙).
시즌 1 = 20부, 시즌 2 부터 15부.
"""
from __future__ import annotations

VERSION = "1.0.0"

S1_LEN, SN_LEN = 20, 15


def locate(e: int) -> tuple[int, int, int]:
    """일지번호 E → (시즌 n, 위치 k, 길이 L)."""
    if e < 1:
        raise ValueError("episode number must be >= 1")
    if e <= S1_LEN:
        return 1, e, S1_LEN
    q, r = divmod(e - S1_LEN - 1, SN_LEN)
    return 2 + q, r + 1, SN_LEN


def is_finale(e: int) -> bool:
    _, k, length = locate(e)
    return k == length
