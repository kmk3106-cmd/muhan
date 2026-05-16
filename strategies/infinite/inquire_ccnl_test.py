# -*- coding: utf-8 -*-
"""
로컬 테스트: KIS 해외주식 체결내역(ccnl) 조회
실행: infinite_buy_v22 폴더에서 python inquire_ccnl_test.py [TQQQ]
"""
import sys
from pathlib import Path

# 프로젝트 루트를 path에 추가
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime, timedelta
import pandas as pd

from .config import KIS_DEVL_YAML, CTAC_TLNO, TRADING_MODE, DATABASE_URL
from .kis_client import get_shared_client
from .settings_store import get_kis_settings


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "TQQQ"
    today = datetime.now().strftime("%Y%m%d")
    dt_start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

    print(f"=== 체결내역 조회 테스트 ===")
    print(f"종목: {ticker}")
    print(f"기간: {dt_start} ~ {today}")
    print()

    # 설정 로드 (DB 우선, 없으면 yaml)
    cfg = get_kis_settings(DATABASE_URL)
    if not cfg and KIS_DEVL_YAML.exists():
        import yaml
        with open(KIS_DEVL_YAML, encoding="UTF-8") as f:
            cfg = yaml.safe_load(f)
    if not cfg:
        print("오류: KIS 설정 없음. 대시보드에서 설정 저장하거나 kis_devlp.yaml 준비")
        return 1

    trading_mode = cfg.get("trading_mode") or TRADING_MODE
    ctac = cfg.get("ctac_tlno") or CTAC_TLNO
    client = get_shared_client(config_dict=cfg, env_dv=trading_mode)

    if not client.auth(ctac):
        print("오류: KIS API 인증 실패")
        return 1
    print("KIS 인증 OK\n")

    # 1) OVRS_EXCG_CD 별 개별 조회 (NASD, NYSE, AMEX)
    print("--- 1) NASD/NYSE/AMEX 개별 조회 ---")
    all_parts = []
    for ovrs in ["NASD", "NYSE", "AMEX"]:
        df = client.inquire_ccnl(
            pdno=ticker,
            ord_strt_dt=dt_start,
            ord_end_dt=today,
            sll_buy_dvsn="00",
            ccld_nccs_dvsn="01",
            ovrs_excg_cd=ovrs,
            ctac_tlno=ctac,
        )
        n = len(df)
        print(f"  {ovrs}: {n}건")
        if not df.empty:
            all_parts.append(df)

    if all_parts:
        merged = pd.concat(all_parts, ignore_index=True)
        # 여러 거래소 병합 시에만 중복제거 (단일 거래소=부분체결 여러 건 보존)
        if len(all_parts) > 1:
            odno_col = next((c for c in ["odno", "ODNO", "ORGN_ODNO"] if c in merged.columns), None)
            if odno_col:
                merged = merged.drop_duplicates(subset=[odno_col], keep="first")
        print(f"  병합(중복제거 후): {len(merged)}건\n")
    else:
        merged = pd.DataFrame()
        print("  병합: 0건 (전체 비어있음)\n")

    # 2) ovrs_excg_cd="%" 단일 조회
    print("--- 2) OVRS_EXCG_CD=% 단일 조회 ---")
    df_pct = client.inquire_ccnl(
        pdno=ticker,
        ord_strt_dt=dt_start,
        ord_end_dt=today,
        sll_buy_dvsn="00",
        ccld_nccs_dvsn="01",
        ovrs_excg_cd="%",
        ctac_tlno=ctac,
    )
    print(f"  %: {len(df_pct)}건\n")

    # 3) 일별거래내역 (inquire-period-trans) - 정산/반영 시점이 다를 수 있음
    print("--- 3) 일별거래내역 (inquire_period_trans) ---")
    df_period = client.inquire_period_trans(
        inqr_strt_dt=dt_start,
        inqr_end_dt=today,
        pdno=ticker,
        sll_buy_dvsn="00",
        ovrs_excg_cd="%",
        ctac_tlno=ctac,
    )
    print(f"  건수: {len(df_period)}건")
    if len(df_period) == 0:
        # 디버그: 원시 API 응답 확인
        cano, acnt_prdt_cd = client._get_account()
        tr_id = "VTTT3012R" if (cfg.get("trading_mode") or "real") == "demo" else "JTTT3012R"
        raw = client._request(
            "GET", "/uapi/overseas-stock/v1/trading/inquire-period-trans", tr_id,
            params={
                "CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                "INQR_STRT_DT": dt_start, "INQR_END_DT": today,
                "SLL_BUY_DVSN": "00", "OVRS_EXCG_CD": "%",
                "TR_CRCY_CD": "USD", "PDNO": ticker,
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            },
            ctac_tlno=ctac,
        )
        print("  [디버그] rt_cd=%s msg1=%s" % (raw.get("rt_cd"), raw.get("msg1", "")))
        for k in ["output", "output2", "output3"]:
            v = raw.get(k)
            if v is not None:
                n = len(v) if isinstance(v, list) else ("dict" if isinstance(v, dict) else type(v).__name__)
                print("  [디버그] %s: %s" % (k, n))
    print()
    if not df_period.empty:
        pcols = [c for c in ["ord_dt", "sll_buy_dvsn_cd", "ft_ccld_qty", "ccld_qty",
                             "ccld_unpr", "ft_ccld_unpr3", "odno", "ODNO", "tot_ccld_amt"] if c in df_period.columns]
        if not pcols:
            pcols = list(df_period.columns)[:12]
        print("  [일별거래내역 목록]")
        print(df_period[pcols].head(30).to_string())
        if len(df_period) > 30:
            print(f"  ... 외 {len(df_period) - 30}건")
        print()

    # 4) 90일 + 최근7일 병합 (체결 많은 종목 최근 누락 보정)
    dt_recent = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    print("--- 4) 90일+최근7일 병합 (TQQQ 등 보정) ---")
    recent_parts = []
    for ovrs in ["NASD", "NYSE", "AMEX"]:
        df = client.inquire_ccnl(
            pdno=ticker, ord_strt_dt=dt_recent, ord_end_dt=today,
            sll_buy_dvsn="00", ccld_nccs_dvsn="01", ovrs_excg_cd=ovrs, ctac_tlno=ctac,
        )
        if not df.empty:
            recent_parts.append(df)
    merged_recent = pd.concat(recent_parts, ignore_index=True) if recent_parts else pd.DataFrame()
    if len(recent_parts) > 1:
        odno_col = next((c for c in ["odno", "ODNO", "ORGN_ODNO"] if c in merged_recent.columns), None)
        if odno_col:
            merged_recent = merged_recent.drop_duplicates(subset=[odno_col], keep="first")
    if not merged_recent.empty and not merged.empty:
        key_cols = [c for c in ["odno", "ODNO", "ord_dt", "ORD_DT", "ft_ccld_qty", "ccld_qty"]
                   if c in merged.columns and c in merged_recent.columns]
        if not key_cols:
            key_cols = [c for c in ["odno", "ODNO", "ORGN_ODNO"] if c in merged.columns and c in merged_recent.columns]
        if key_cols:
            combined = pd.concat([merged, merged_recent], ignore_index=True)
            merged = combined.drop_duplicates(subset=key_cols, keep="last")
            print(f"  최근7일 병합 후: {len(merged)}건\n")
    elif not merged_recent.empty and merged.empty:
        merged = merged_recent
        print(f"  최근7일만 사용: {len(merged)}건\n")
    else:
        print(f"  최근7일 조회: {len(merged_recent)}건\n")

    # 결과 표시용 DataFrame (병합 결과 우선)
    result = merged if not merged.empty else df_pct

    if result.empty:
        print("체결내역 0건. 기간/종목 확인.")
        return 0

    # 주요 컬럼 표시
    cols = ["odno", "ODNO", "ORGN_ODNO", "ord_dt", "sll_buy_dvsn_cd", "ft_ccld_qty", "ccld_qty",
            "ccld_unpr", "ft_ccld_unpr3", "ord_unpr", "ovrs_ord_unpr", "ord_dvsn_cd"]
    avail = [c for c in cols if c in result.columns]
    if not avail:
        avail = list(result.columns[:12])

    print("--- 체결 목록 (최근순) ---")
    print(result[avail].head(30).to_string())
    print()
    if len(result) > 30:
        print(f"... 외 {len(result) - 30}건")

    # odno 통계
    odno_vals = []
    for c in ["odno", "ODNO", "ORGN_ODNO"]:
        if c in result.columns:
            odno_vals.extend(result[c].dropna().astype(str).tolist())
    if odno_vals:
        uniq = set(v.strip() for v in odno_vals if v and str(v).strip())
        print(f"\nodno 샘플: {list(uniq)[:10]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
