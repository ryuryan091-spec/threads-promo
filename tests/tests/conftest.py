"""공통 픽스처.

v1.6.0 계정 보호(안전) 모드는 기본값이 '자동화 꺼짐 · 예산 2 · 링크 리플 0% · 정형 문구 꺼짐 ·
답글 캡 축소'다. v1.5.0 까지의 테스트는 자동화가 켜져 있고 이전 캡을 쓰는 상태를 전제로 쓰였다.
그 전제를 테스트마다 고쳐 쓰지 않고, 여기서 v1.5.0 동작 프로필(legacy)을 자동 적용한다.

  - 안전 모드 자체를 검사하는 테스트는 `@pytest.mark.safety_defaults` 를 붙이거나
    test_safety_v16.py 처럼 모듈 이름으로 제외되어 코드 기본값을 그대로 본다.
  - 환경변수와 config 상수를 함께 고정한다. config 는 import 시점에 환경변수를 읽고,
    일부 테스트는 importlib.reload(config) 를 하므로 둘 다 맞춰야 실행 순서와 무관해진다.
  - 회로 차단기(safety._TRIPPED)는 프로세스 단위 상태라 테스트마다 초기화한다.
"""

from __future__ import annotations

import pytest

# v1.5.0 동작과 같아지는 값. 예산은 사실상 무제한(이전에는 총량 예산이 없었다).
LEGACY_PROFILE: dict[str, object] = {
    "AUTOMATION_ENABLED": True,
    "DAILY_POST_BUDGET": 1000,
    "LINK_REPLY_PCT": 100,
    "WARMUP_UNTIL": "",
    "REPLY_CANNED_ENABLED": True,
    "REPLY_DAILY_CAP": 40,
    "REPLY_AUTHOR_DAILY_CAP": 3,
    "REPLY_THREAD_AUTHOR_CAP": 4,
    "REPLY_PER_RUN_CAP": 4,
    "REPLY_SCHEDULED_RUN_CAP": 6,
}

# 코드 기본값(안전 프로필)을 그대로 보는 모듈
SAFETY_MODULES = frozenset({"test_safety_v16"})


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "safety_defaults: v1.6.0 안전 기본값을 그대로 쓴다(legacy 프로필 미적용)"
    )


def _env_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@pytest.fixture(autouse=True)
def _reset_circuit():
    from src import safety

    safety.reset_circuit()
    yield
    safety.reset_circuit()


@pytest.fixture(autouse=True)
def _legacy_profile(request, monkeypatch):
    module = request.node.module.__name__.rsplit(".", 1)[-1]
    if module in SAFETY_MODULES or request.node.get_closest_marker("safety_defaults"):
        yield
        return

    from src import config

    for key, value in LEGACY_PROFILE.items():
        monkeypatch.setenv(key, _env_value(value))
        monkeypatch.setattr(config, key, value)
    yield
