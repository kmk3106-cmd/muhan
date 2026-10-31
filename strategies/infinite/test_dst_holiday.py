# -*- coding: utf-8 -*-
"""무한매수법 DST 재스케줄 + 휴장일 체크 검증."""
import ast, sys, types, logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/user/muhan")
ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
logger = logging.getLogger("t"); logger.addHandler(logging.NullHandler())
fails = []
def ck(name, got, want):
    ok = got == want
    print(("  OK  " if ok else "  FAIL") + f" {name}: {got}" + ("" if ok else f"  (기대 {want})"))
    if not ok: fails.append(name)

def grab(path, names, ns):
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(n, ast.FunctionDef) and n.name in names:
            exec(compile(ast.Module([n], []), "<x>", "exec"), ns)
    return ns

# ── 1. 휴장일 표: 무한매수법 == 떨사오팔 ──────────────────────────────
print("[1] 휴장일 표가 떨사오팔과 완전히 같은가 (2024~2030)")
a = grab("strategies/infinite/worker.py", {"_easter", "_us_market_holidays"},
         {"datetime": datetime, "timedelta": timedelta})
b = grab("strategies/ddsop/worker.py", {"_easter", "_us_market_holidays"},
         {"datetime": datetime, "timedelta": timedelta})
for y in range(2024, 2031):
    ck(f"{y}년", sorted(map(str, a["_us_market_holidays"](y))),
                 sorted(map(str, b["_us_market_holidays"](y))))

# ── 2. is_us_trading_day: 날짜를 바꿔가며 ────────────────────────────
print("\n[2] is_us_trading_day — 요일 + 휴장일")
class FakeDT:
    fixed = None
    @classmethod
    def now(cls, tz=None): return cls.fixed
    @classmethod
    def utcnow(cls): return cls.fixed
ns = grab("strategies/infinite/worker.py",
          {"_easter", "_us_market_holidays", "is_us_trading_day"},
          {"datetime": FakeDT, "timedelta": timedelta})
f = ns["is_us_trading_day"]
cases = [
    (date(2026, 11, 2),  True,  "11/02 월 — EST 첫 거래일"),
    (date(2026, 11, 26), False, "11/26 목 — 추수감사절 휴장"),
    (date(2026, 11, 27), True,  "11/27 금 — 블프(13:00 조기폐장, 휴장 아님)"),
    (date(2026, 12, 24), True,  "12/24 목 — 조기폐장(휴장 아님)"),
    (date(2026, 12, 25), False, "12/25 금 — 크리스마스 휴장"),
    (date(2026, 11, 1),  False, "11/01 일 — 주말"),
    (date(2027, 1, 1),   False, "27/01/01 금 — 신정 휴장"),
    (date(2027, 3, 26),  False, "27/03/26 금 — Good Friday 휴장"),
]
for d, want, label in cases:
    FakeDT.fixed = datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET)
    ck(label, f(), want)

# ── 3. DST 전환 전후 실행시각 ────────────────────────────────────────
print("\n[3] 실행시각 계산 (장개시+30분 ET → KST)")
w = grab("strategies/infinite/worker.py", {"get_us_market_run_time_kst"},
         {"datetime": FakeDT, "timedelta": timedelta})
g = w["get_us_market_run_time_kst"]
for d, want, label in [(date(2026,10,30), (23,0), "써머타임 중(EDT)"),
                       (date(2026,11,2),  (0,0),  "써머타임 종료 후(EST)")]:
    FakeDT.fixed = datetime(d.year, d.month, d.day, 19, 0, tzinfo=ET)
    ck(f"{d} {label}", g(), want)

# ── 4. _reschedule_worker_for_dst 가 잡을 옮기는가 ───────────────────
print("\n[4] reschedule_dst — 7개 잡(pre_auth + worker_0~5)을 EST 로 이동")
moved = {}
class FakeSched:
    def reschedule_job(self, jid, trigger=None, hour=None, minute=None):
        moved[jid] = (hour, minute)
rns = {"logger": logger, "scheduler": FakeSched(),
       "get_us_market_run_time_kst": lambda: (0, 0)}
grab("strategies/infinite/main.py", {"_reschedule_worker_for_dst"}, rns)
rns["_reschedule_worker_for_dst"]()
ck("옮긴 잡 개수", len(moved), 7)
ck("pre_auth (00:00 의 5분 전 = 23:55)", moved.get("pre_auth"), (23, 55))
ck("worker_0~5", [moved.get(f"worker_{i}") for i in range(6)],
   [(0,0),(1,0),(2,0),(3,0),(4,0),(5,0)])

print("\n[5] 계산 실패 시 기존 스케줄 유지 (잡을 건드리지 않음)")
moved.clear()
rns["get_us_market_run_time_kst"] = lambda: (_ for _ in ()).throw(RuntimeError("tz 오류"))
rns["_reschedule_worker_for_dst"]()
ck("잡 변경 0건", len(moved), 0)

print("\n" + ("전부 통과" if not fails else f"실패 {len(fails)}건: {fails}"))
sys.exit(1 if fails else 0)
