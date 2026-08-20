# -*- coding: utf-8 -*-
"""VR(밸류리밸런싱) 전역 설정 — NH PLUG 브로커 전용.

NH 자격은 레포 밖 파일에서 로드:
  1) VR_NH_ENV 환경변수 지정 경로
  2) /root/trading_suite_state/nh_plug.env  (서버 표준 — 배포와 무관하게 영속)
  3) ~/.nhplug/.env                          (SDK 표준 — 로컬 개발)
형식: KEY=VALUE 줄들 (NHPLUG_APP_KEY / NHPLUG_APP_SECRET / NHPLUG_BASE_URL / NHPLUG_AUTH_URL)
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATABASE_PATH = Path(os.getenv("VR_DATABASE_PATH", str(BASE_DIR / "vr.db")))

KILL_SWITCH_FILE = BASE_DIR / ".kill_switch"

_ENV_CANDIDATES = [
    os.getenv("VR_NH_ENV", ""),
    "/root/trading_suite_state/nh_plug.env",
    str(Path.home() / ".nhplug" / ".env"),
]


def load_nh_env() -> str | None:
    """NH 자격 env 파일을 os.environ 에 로드(기존 환경변수 우선). 로드된 파일 경로 반환."""
    for cand in _ENV_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
                return str(p)
            except Exception:
                continue
    return None
