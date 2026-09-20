"""threads-promo 패키지.

import 시 로그 자격증명 마스킹을 1회 설치한다(src.redact 참고).
어느 엔트리포인트로 실행해도 src 패키지를 거치므로 한 곳에서 보장된다.
"""

from . import redact as _redact

_redact.install()
