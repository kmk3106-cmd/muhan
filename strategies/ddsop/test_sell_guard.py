# -*- coding: utf-8 -*-
"""매도 가용수량 가드 검증 — worker.py 에서 두 함수 소스를 그대로 떼어 실행."""
import ast, logging, sys, types
import pandas as pd

logger = logging.getLogger("t"); logger.addHandler(logging.NullHandler())

src = open("strategies/ddsop/worker.py", encoding="utf-8").read()
tree = ast.parse(src)
ns = {"pd": pd, "logger": logger, "KISClient": object}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in ("_get_sellable_qty", "_fit_sells_to_capacity"):
        exec(compile(ast.Module([node], []), "<w>", "exec"), ns)
fit, sellable_of = ns["_fit_sells_to_capacity"], ns["_get_sellable_qty"]

class O:
    def __init__(s, n, t, p, q): s.tranche_num, s.order_type, s.price, s.qty, s.side = n, t, p, q, "sell"
    def __repr__(s): return f"T{s.tranche_num}/{s.order_type}/{s.price}/{s.qty}"

fails = []
def ck(name, got, want):
    ok = got == want
    print(("  OK  " if ok else "  FAIL") + f" {name}: {got}" + ("" if ok else f" (기대 {want})"))
    if not ok: fails.append(name)

print("[1] _fit_sells_to_capacity — 실사고 SPXL 재현 (T1~T3 각 2주, 가용 0)")
o = [O(1,"LOC",300.81,2), O(2,"LOC",296.46,2), O(3,"LOC",288.99,2)]
k, d = fit(o, 0); ck("가용 0 → 전건 보류", len(d), 3)

print("[2] 가용 4주 → 낮은가 2건만 (체결 가능성 높은 순)")
k, d = fit(o, 4)
ck("제출 2건", len(o)-len(d), 2)
ck("남은 건 최고가 T1", [x.tranche_num for x in d], [1])

print("[3] 손절(MOC) 최우선 — 가용 2주, MOC 2주 + LOC 2주")
o2 = [O(1,"LOC",300.81,2), O(2,"MOC",0.0,2)]
k, d = fit(o2, 2)
ck("MOC 채택", [x.tranche_num for x in o2 if id(x) in k], [2])

print("[4] 정확히 맞으면 전건 통과")
k, d = fit(o, 6); ck("보류 0건", len(d), 0)

print("[5] 쪼개지 않는다 — 가용 3주, 각 2주")
k, d = fit(o, 3); ck("1건만 제출", len(o)-len(d), 1)

print("[6] 음수·0 수량 방어")
k, d = fit([O(1,"LOC",10.0,0), O(2,"LOC",11.0,-1)], 5); ck("둘 다 미채택", len(d), 2)

print("[7] _get_sellable_qty — ord_psbl_qty 를 읽는다 (보유≠가용)")
class C:
    def __init__(s, frames): s.f = frames
    def inquire_balance(s, ovrs_excg_cd="NASD", ctac_tlno=""):
        return s.f.get(ovrs_excg_cd, pd.DataFrame()), pd.DataFrame()
nasd = pd.DataFrame([{"ovrs_pdno":"SPXL","ovrs_cblc_qty":"6","ord_psbl_qty":"0"}])
ck("(가용,보유)", sellable_of(C({"NASD":nasd}), "SPXL", ""), (0, 6))

print("[8] 다른 거래소에 있어도 찾는다")
nyse = pd.DataFrame([{"ovrs_pdno":"TECL","ovrs_cblc_qty":"9","ord_psbl_qty":"3"}])
ck("NYSE 매칭", sellable_of(C({"NASD":pd.DataFrame(),"NYSE":nyse}), "TECL", ""), (3, 9))

print("[9] 잔고에 종목이 없으면 None → 가드 미적용(기존 동작 유지)")
ck("None 반환", sellable_of(C({"NASD":nasd}), "TECL", ""), None)

print("[10] 컬럼 명세가 다르면 None → 가드 미적용")
odd = pd.DataFrame([{"pdno":"SPXL","qty":"6"}])
ck("None 반환", sellable_of(C({"NASD":odd}), "SPXL", ""), None)

print("[11] 조회 예외에도 죽지 않는다")
class Boom:
    def inquire_balance(s, **k): raise RuntimeError("API down")
ck("None 반환", sellable_of(Boom(), "SPXL", ""), None)

print("\n" + ("전부 통과" if not fails else f"실패 {len(fails)}건: {fails}"))
sys.exit(1 if fails else 0)
