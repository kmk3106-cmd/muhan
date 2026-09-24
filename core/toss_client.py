# -*- coding: utf-8 -*-
"""토스증권 Open API — **조회 전용** 클라이언트.

주문·정정·취소는 의도적으로 구현하지 않는다. 토스 계좌의 매매는 토스 앱의 자동모으기가
담당하고, 이 플랫폼은 자산 합산만 한다. (주문 기능이 필요해지면 그때 설계·승인 후 추가)

자격 파일: /root/trading_suite_state/toss.env (배포와 무관하게 보존되는 디렉터리)
    TOSS_CLIENT_ID / TOSS_CLIENT_SECRET / TOSS_ACCOUNT_NO / TOSS_BASE_URL

공식 제약 (developers.tossinvest.com FAQ):
- 토큰은 **클라이언트당 1개**만 유효하다. 새로 발급하면 직전 토큰이 즉시 무효가 되므로
  프로세스 안에서 하나를 캐시해 공유한다(만료 60초 전 갱신).
- 계좌·자산 API 는 `X-Tossinvest-Account` 헤더에 **accountSeq**(계좌번호 아님)를 넣는다.
- 허용 IP 에 등록된 고정 IP 에서만 호출된다(미등록 시 403 access_denied).
- 호출 한도: 인증 5/초, 시세 15/초, 주문 10/초.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger("trading_suite.toss")

_ENV_CANDIDATES = [
    os.getenv("TOSS_ENV", ""),
    "/root/trading_suite_state/toss.env",
    str(Path.home() / ".toss" / ".env"),
]

_CACHE_FILE = Path(__file__).resolve().parent / "_toss.json"
SNAPSHOT_TTL = 600           # 스냅샷 갱신 주기(초) — 대시보드는 캐시만 읽는다
_token: dict = {"value": "", "exp": 0.0}


def load_env() -> dict:
    """자격 로드 (환경변수 우선). 없으면 빈 dict."""
    env = {}
    for cand in _ENV_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
            break
        except Exception as e:
            logger.warning(f"[toss] 자격 파일 읽기 실패 {p}: {e}")
    for k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_NO", "TOSS_BASE_URL"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    env.setdefault("TOSS_BASE_URL", "https://openapi.tossinvest.com")
    return env


def configured() -> bool:
    e = load_env()
    return bool(e.get("TOSS_CLIENT_ID") and e.get("TOSS_CLIENT_SECRET"))


def _request(url: str, headers: dict | None = None, form: dict | None = None, timeout: int = 20):
    hdr = dict(headers or {})
    body = None
    method = "GET"
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdr["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    req = urllib.request.Request(url, data=body, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, json.loads(raw or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            if e.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        except Exception:
            pass
        txt = raw.decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"error": {"message": txt[:200]}}


def token(force: bool = False) -> str:
    """액세스 토큰 (프로세스 내 1개 공유, 만료 60초 전 갱신)."""
    if not force and _token["value"] and time.time() < _token["exp"]:
        return _token["value"]
    env = load_env()
    if not (env.get("TOSS_CLIENT_ID") and env.get("TOSS_CLIENT_SECRET")):
        raise RuntimeError("토스 자격 미설정")
    st, body = _request(env["TOSS_BASE_URL"] + "/oauth2/token", form={
        "grant_type": "client_credentials",
        "client_id": env["TOSS_CLIENT_ID"],
        "client_secret": env["TOSS_CLIENT_SECRET"],
    })
    if st != 200 or not body.get("access_token"):
        raise RuntimeError(f"토큰 발급 실패 {st}: {json.dumps(body, ensure_ascii=False)[:160]}")
    _token["value"] = body["access_token"]
    _token["exp"] = time.time() + max(60, int(body.get("expires_in") or 3600)) - 60
    return _token["value"]


def _get(path: str, seq: int | None = None) -> dict:
    env = load_env()
    hdr = {"Authorization": "Bearer " + token()}
    if seq is not None:
        hdr["X-Tossinvest-Account"] = str(seq)
    st, body = _request(env["TOSS_BASE_URL"] + path, headers=hdr)
    if st == 401:                       # 토큰 무효(재발급·revoke) → 1회 재시도
        hdr["Authorization"] = "Bearer " + token(force=True)
        st, body = _request(env["TOSS_BASE_URL"] + path, headers=hdr)
    if st != 200:
        raise RuntimeError(f"{path} {st}: {json.dumps(body, ensure_ascii=False)[:160]}")
    return body.get("result", body)


def accounts() -> list[dict]:
    r = _get("/api/v1/accounts")
    return r if isinstance(r, list) else (r.get("result") or [])


def holdings(seq: int) -> dict:
    return _get("/api/v1/holdings", seq=seq)


def buying_power(seq: int, currency: str) -> dict:
    return _get(f"/api/v1/buying-power?currency={currency}", seq=seq)


def _f(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return 0.0


def load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(d: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[toss] 캐시 저장 실패: {e}")


def refresh(force: bool = False) -> dict:
    """토스 계좌 스냅샷 갱신 (조회만). 실패하면 직전 캐시를 그대로 둔다."""
    cur = load_cache()
    if not force and cur.get("ts"):
        try:
            if time.time() - float(cur.get("epoch") or 0) < SNAPSHOT_TTL:
                return cur
        except Exception:
            pass
    if not configured():
        return cur
    try:
        accs = accounts()
        if not accs:
            raise RuntimeError("계좌 없음")
        a = accs[0]
        seq = int(a.get("accountSeq") or 1)
        h = holdings(seq)
        mv = (h.get("marketValue") or {}).get("amount") or {}
        pl = (h.get("profitLoss") or {}).get("amount") or {}
        items = []
        for it in (h.get("items") or []):
            qty = _f(it.get("quantity"))
            m = it.get("marketValue") or {}
            p = it.get("profitLoss") or {}
            buy = _f(m.get("purchaseAmount"))
            ev = _f(m.get("amount"))
            items.append({
                "ticker": it.get("symbol"), "name": it.get("name"),
                "market": it.get("marketCountry"), "currency": it.get("currency"),
                "qty": int(qty) if qty == int(qty) else qty,
                "avg_price": _f(it.get("averagePurchasePrice")),
                "now_price": _f(it.get("lastPrice")),
                "buy_amt": round(buy, 2), "eval_amt": round(ev, 2),
                "pnl": round(_f(p.get("amount")), 2),
                "pnl_rt": round(_f(p.get("rate")) * 100, 2),
            })
        cash_usd = _f((buying_power(seq, "USD") or {}).get("cashBuyingPower"))
        cash_krw = _f((buying_power(seq, "KRW") or {}).get("cashBuyingPower"))
        fx = 0.0                        # 원화 예수금을 달러로 환산할 때만 쓴다 (실패해도 진행)
        try:
            fx = _f((_get("/api/v1/exchange-rate?baseCurrency=USD&quoteCurrency=KRW") or {}).get("rate"))
        except Exception as e:
            logger.warning(f"[toss] 환율 조회 실패(원화 환산 생략): {e}")
        snap = {
            "ok": True,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "account_no": a.get("accountNo"), "account_seq": seq,
            "items": items,
            "buy_usd": round(_f((h.get("totalPurchaseAmount") or {}).get("usd")), 2),
            "eval_usd": round(_f(mv.get("usd")), 2),
            "pnl_usd": round(_f(pl.get("usd")), 2),
            "cash_usd": round(cash_usd, 2),
            "cash_krw": round(cash_krw, 2),
            "fx": fx,
            "cash_krw_usd": round(cash_krw / fx, 2) if fx > 0 else 0.0,
            "error": "",
        }
        _save_cache(snap)
        logger.info(f"[toss] 스냅샷 갱신: 평가 ${snap['eval_usd']:,.2f} · 예수금 ${snap['cash_usd']:,.2f} "
                    f"· 종목 {len(items)}개")
        return snap
    except Exception as e:
        logger.warning(f"[toss] 스냅샷 갱신 실패(직전 캐시 유지): {e}")
        if cur:
            cur = {**cur, "error": str(e)[:200]}
            _save_cache(cur)
            return cur
        return {"ok": False, "ts": "", "items": [], "error": str(e)[:200]}
