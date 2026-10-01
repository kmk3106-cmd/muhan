# -*- coding: utf-8 -*-
import json, pathlib, sys, importlib
sys.path.insert(0, "/home/user/muhan")
import core.equity_snapshot as es

tmp = pathlib.Path("/tmp/claude-0/-home-user/bd520bf5-bec5-5361-9b89-a418b8d461f8/scratchpad/_eq_test.jsonl")
es._FILE = tmp
fails = []
def run(name, rows, expect_sub):
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows), encoding="utf-8")
    d = es.diagnose()
    ok = expect_sub in d["verdict"]
    print(("  OK  " if ok else "  FAIL")+f" {name}\n        → {d['verdict']}")
    if not ok: fails.append(name)
    return d

def pt(day, hh, v): return {"ts": f"2026-09-{day:02d}T{hh:02d}:00:00+09:00", "total_assets": v,
                            "net_invested": 0, "pnl": 0, "realized": {}}

print("[a] 날짜가 2일치뿐 → 직선")
run("2일치", [pt(29,9,50000), pt(29,18,50100), pt(30,9,50200), pt(30,18,50300)], "날짜 2일치")

print("[b] 날짜별 값이 전부 동일 → 계좌요약 미갱신")
run("동일값", [pt(d,18,52840.19) for d in range(20,30)], "같은 값")

print("[c] 전부 0")
run("0원", [pt(d,18,0) for d in range(20,30)], "전부 0")

print("[d] 정상적으로 변하는 데이터")
d = run("정상", [pt(i,18,50000+i*120) for i in range(10,28)], "데이터는 변하고 있다")
print(f"        days={d['days']} distinct={d['daily_distinct_values']} recent={len(d['recent'])}")

print("[e] 파일 없음")
tmp.unlink(missing_ok=True)
d = es.diagnose()
print(("  OK  " if "한 건도 없음" in d["verdict"] else "  FAIL")+f" 빈 파일\n        → {d['verdict']}")
if "한 건도 없음" not in d["verdict"]: fails.append("빈 파일")

print("[f] 스냅샷 실패가 진단에 노출되는가")
es._LAST_ERR["msg"] = "KeyError: tot_evlu"; es._LAST_ERR["at"] = "2026-10-01T07:00:00+09:00"
d = es.diagnose()
ok = d["last_snapshot_error"] == "KeyError: tot_evlu"
print(("  OK  " if ok else "  FAIL")+f" 실패 사유 노출: {d['last_snapshot_error']}")
if not ok: fails.append("실패 노출")

print("\n"+("전부 통과" if not fails else f"실패: {fails}"))
sys.exit(1 if fails else 0)
