# -*- coding: utf-8 -*-
"""계좌 보유종목 상세 캐시 (종목별 매입단가·현재가·평가수익률).

워커가 동기화 때 이미 받아오는 KIS 해외주식 잔고(df1)의 종목별 행을
정규화해 저장만 한다. **추가 KIS 호출 없음** — 기존 데이터 재사용.
단일 공용계좌(69567573)라 어느 전략 워커가 갱신하든 전 종목이 들어온다.

저장: core/_holdings.json (배포별 보존 대상, gitignore)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover
    _KST = None

_FILE = Path(__file__).resolve().parent / "_holdings.json"


def _now_kst_iso() -> str:
    return (datetime.now(_KST) if _KST else datetime.now()).isoformat(timespec="seconds")


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return 0.0


def _i(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return 0


def save_from_balance_rows(rows: list[dict], source: str = "") -> dict | None:
    """KIS 해외주식 잔고 레코드(list[dict]) → 정규화 보유종목 저장.

    rows: df1.to_dict('records') (cano/ovrs_pdno/pchs_avg_pric/ovrs_cblc_qty/
          now_pric2/ovrs_stck_evlu_amt/frcr_pchs_amt1/frcr_evlu_pfls_amt/evlu_pfls_rt/ovrs_excg_cd ...)
    """
    try:
        items = []
        for r in rows or []:
            tkr = str(r.get("ovrs_pdno") or r.get("OVRS_PDNO") or "").strip().upper()
            qty = _i(r.get("ovrs_cblc_qty", r.get("OVRS_CBLC_QTY")))
            if not tkr or qty <= 0:
                continue
            items.append({
                "ticker": tkr,
                "name": str(r.get("ovrs_item_name") or "").strip(),
                "qty": qty,
                "avg_price": round(_f(r.get("pchs_avg_pric")), 4),
                "now_price": round(_f(r.get("now_pric2")), 4),
                "eval_amt": round(_f(r.get("ovrs_stck_evlu_amt")), 2),
                "buy_amt": round(_f(r.get("frcr_pchs_amt1")), 2),
                "pnl": round(_f(r.get("frcr_evlu_pfls_amt")), 2),
                "pnl_rt": round(_f(r.get("evlu_pfls_rt")), 2),
                "excg": str(r.get("ovrs_excg_cd") or "").strip(),
            })
        payload = {"ts": _now_kst_iso(), "source": source, "items": items}
        _FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload
    except Exception:
        return None


def load() -> dict:
    try:
        d = json.loads(_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            d.setdefault("ts", "")
            return d
    except Exception:
        pass
    return {"ts": "", "source": "", "items": []}
