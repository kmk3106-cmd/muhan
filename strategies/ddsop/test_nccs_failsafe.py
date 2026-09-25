# -*- coding: utf-8 -*-
"""미체결 조회 실패/누락이 '주문 없음'으로 둔갑하지 않는지 검증.
kis_client.py·worker.py 소스에서 해당 함수만 그대로 떼어 실행한다."""
import ast, logging, sys, types
import pandas as pd

logger = logging.getLogger("t"); logger.addHandler(logging.NullHandler())
fails = []
def ck(name, got, want):
    ok = got == want
    print(("  OK  " if ok else "  FAIL")+f" {name}: {got}"+("" if ok else f" (기대 {want})"))
    if not ok: fails.append(name)

def grab(path, names, ns):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef,)) and node.name in names:
            exec(compile(ast.Module([node], []), "<x>", "exec"), ns)
    return ns

# ---------- inquire_nccs_ex ----------
ns = {"pd": pd, "logger": logger, "time": types.SimpleNamespace(sleep=lambda s: None)}
grab("strategies/ddsop/kis_client.py", {"inquire_nccs_ex", "inquire_nccs"}, ns)

class FakeClient:
    env_dv = "real"
    def __init__(self, responses): self.r = list(responses); self.calls = 0
    def _get_account(self): return ("123", "01")
    def _request(self, *a, **k):
        self.calls += 1
        return self.r.pop(0) if self.r else {"rt_cd": "0", "output": []}
    inquire_nccs_ex = ns["inquire_nccs_ex"]
    inquire_nccs = ns["inquire_nccs"]

print("[1] 조회 실패는 '미체결 없음'과 구분된다")
c = FakeClient([{"rt_cd": "1", "msg1": "일시 오류"}])
ok, df = c.inquire_nccs_ex()
ck("실패 → ok=False", ok, False); ck("실패 → 빈 결과", df.empty, True)

print("[2] 정말 미체결이 없을 때는 성공으로 본다")
c = FakeClient([{"rt_cd": "0", "output": []}])
ok, df = c.inquire_nccs_ex()
ck("빈 목록 → ok=True", ok, True); ck("빈 결과", df.empty, True)

print("[3] 연속조회 — 뒷장까지 모아온다 (한 장만 받고 끊지 않는다)")
c = FakeClient([
 {"rt_cd":"0","output":[{"odno":"1"},{"odno":"2"}],"tr_cont":"M","ctx_area_fk200":"F","ctx_area_nk200":"N"},
 {"rt_cd":"0","output":[{"odno":"3"}],"tr_cont":"D","ctx_area_fk200":"","ctx_area_nk200":""}])
ok, df = c.inquire_nccs_ex()
ck("ok=True", ok, True); ck("2장 병합 3건", len(df), 3); ck("호출 2회", c.calls, 2)

print("[4] 뒷장에서 끊기면 '모른다'(ok=False) — 앞장만으로 단정하지 않는다")
c = FakeClient([
 {"rt_cd":"0","output":[{"odno":"1"}],"tr_cont":"M","ctx_area_fk200":"F","ctx_area_nk200":"N"},
 {"rt_cd":"1","msg1":"조회 오류"}])
ok, df = c.inquire_nccs_ex()
ck("ok=False", ok, False)

print("[5] 기존 호출부 호환 — inquire_nccs 는 여전히 DataFrame")
c = FakeClient([{"rt_cd":"0","output":[{"odno":"9"}]}])
ck("DataFrame 반환", isinstance(c.inquire_nccs(), pd.DataFrame), True)

# ---------- _get_kis_pending_odnos ----------
print("[6] 거래소 하나라도 실패하면 None('모른다')")
wns = {"pd": pd, "logger": logger, "KISClient": object}
grab("strategies/ddsop/worker.py", {"_get_kis_pending_odnos", "_normalize_odno"}, wns)
get_odnos = wns["_get_kis_pending_odnos"]
class C2:
    def __init__(s, per): s.per = per
    def inquire_nccs_ex(s, ovrs_excg_cd="NASD", ctac_tlno=""): return s.per[ovrs_excg_cd]
good = (True, pd.DataFrame([{"pdno":"TECL","odno":"77"}]))
bad  = (False, pd.DataFrame())
empty= (True, pd.DataFrame())
ck("NYSE 실패 → None", get_odnos(C2({"NASD":good,"NYSE":bad,"AMEX":empty}), "TECL", ""), None)
print("[7] 전부 성공하면 odno 집합")
got = get_odnos(C2({"NASD":good,"NYSE":empty,"AMEX":empty}), "TECL", "")
ck("77 포함", "77" in (got or set()), True)
print("[8] 전부 성공 + 미체결 0건 → 빈 집합(None 아님)")
ck("빈 집합", get_odnos(C2({"NASD":empty,"NYSE":empty,"AMEX":empty}), "TECL", ""), set())

# ---------- _sync_pending_with_kis fail-safe ----------
print("[9] 조회 불확실이면 단 한 건도 cancelled 로 바꾸지 않는다 (핵심 회귀)")
sns = {"pd": pd, "logger": logger, "KISClient": object, "Session": object, "Ticker": object,
       "TradeOrder": object,
       "_yesterday_kst": lambda: "20260924",
       "_get_kis_pending_odnos": lambda *a, **k: None,
       "_get_kis_filled_odnos": lambda *a, **k: (_ for _ in ()).throw(AssertionError("여기까지 오면 안 됨")),
       "_normalize_odno": lambda x: x,
       "_log_structured": lambda *a, **k: None,
       "select": lambda *a, **k: (_ for _ in ()).throw(AssertionError("DB 조회하면 안 됨"))}
grab("strategies/ddsop/worker.py", {"_sync_pending_with_kis"}, sns)
class Boom:  # 세션을 건드리면 터지게
    def __getattr__(s, n): raise AssertionError("세션 사용 금지")
try:
    n = sns["_sync_pending_with_kis"](Boom(), object(), types.SimpleNamespace(ticker="TECL"), "20260925", "")
    ck("취소 0건 + DB 미접근", n, 0)
except AssertionError as e:
    ck("취소 0건 + DB 미접근", f"예외: {e}", 0)

print("\n" + ("전부 통과" if not fails else f"실패 {len(fails)}건: {fails}"))
sys.exit(1 if fails else 0)
