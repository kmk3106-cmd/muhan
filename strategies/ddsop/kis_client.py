# -*- coding: utf-8 -*-
"""
한국투자증권 Open API 클라이언트 (해외주식)
토큰 발급, 잔고/체결 조회, 주문, 취소 등 REST API 래핑
(infinite_buy_v22에서 복사)
"""
import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml

logger = logging.getLogger(__name__)

ORD_DVSN_LIMIT = "00"
ORD_DVSN_MOC = "33"
ORD_DVSN_LOC = "34"

TOKEN_CACHE_DIR = Path.home() / ".kis"

_EXCD_MAP = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS", "SEHK": "HKS", "SHAA": "SHS", "SZAA": "SZS", "TKSE": "TSE", "HASE": "HNX", "VNSE": "HSX"}
_VALID_OVRS_EXCG = frozenset({"NASD", "NAS", "NYSE", "AMEX", "SEHK", "SHAA", "SZAA", "TKSE", "HASE", "VNSE"})
_US_EXCD_FALLBACK = ["NYS", "NAS", "AMS"]


def _normalize_ovrs_excg(ovrs_excg_cd: Optional[str]) -> str:
    v = (ovrs_excg_cd or "").strip().upper()
    if v in _VALID_OVRS_EXCG:
        return v
    if v in ("NAD", "NADS", "NASDQ"):
        return "NASD"
    return "NASD"


def _ovrs_to_excd(ovrs_excg_cd: Optional[str]) -> str:
    """주문용(4글자) → 시세용(3글자) EXCD 변환."""
    v = _normalize_ovrs_excg(ovrs_excg_cd)
    return _EXCD_MAP.get(v, "NAS")


def _token_cache_path(env_dv: str, app_key: str) -> Path:
    """설정별 토큰 캐시 파일 경로 (env+app_key 해시)"""
    key = f"{env_dv}:{app_key or 'default'}"
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return TOKEN_CACHE_DIR / f"token_cache_{h}.json"


# ========== 공유 클라이언트 (1분당 1회 토큰 제한 회피) ==========
_shared_client: Optional["KISClient"] = None
_shared_config_key: str = ""
_shared_lock = threading.Lock()


def get_shared_client(
    config_dict: Optional[dict] = None,
    config_path: Optional[Path] = None,
    env_dv: str = "real",
) -> "KISClient":
    """
    KIS API 1분당 1회 토큰 제한 회피용 싱글톤 클라이언트.
    동일 설정 + 유효한 토큰이 있으면 재사용, 없으면 생성.
    """
    global _shared_client, _shared_config_key
    cfg = config_dict if config_dict else {}
    if not cfg and config_path and config_path.exists():
        with open(config_path, encoding="UTF-8") as f:
            cfg = yaml.safe_load(f)
    app_key = (cfg.get("paper_app") or "") if env_dv == "demo" else (cfg.get("my_app") or cfg.get("app_key") or "")
    key = f"{env_dv}:{app_key or 'default'}"

    if _shared_client is not None and _shared_config_key == key:
        return _shared_client

    with _shared_lock:
        if _shared_client is not None and _shared_config_key == key:
            return _shared_client
        client = KISClient(config_path=config_path, config_dict=config_dict or None, env_dv=env_dv)
        _shared_client = client
        _shared_config_key = key
        return client


def reset_shared_client():
    """설정 변경 시 공유 클라이언트 초기화 (다음 요청에서 새로 생성)"""
    global _shared_client, _shared_config_key
    with _shared_lock:
        _shared_client = None
        _shared_config_key = ""


class KISClient:
    def __init__(
        self,
        config_path: Optional[Path] = None,
        config_dict: Optional[dict] = None,
        env_dv: str = "real",
    ):
        self.env_dv = env_dv
        self.config_path = config_path or Path.home() / "KIS" / "config" / "kis_devlp.yaml"
        self._config_dict = config_dict
        self._cfg: Optional[dict] = None
        self._token: Optional[str] = None
        self._token_expired: Optional[datetime] = None
        self._token_issued_at: Optional[datetime] = None
        self._base_headers: dict = {}
        self._auth_lock = threading.Lock()

    def _load_config(self) -> dict:
        if self._cfg is None:
            if self._config_dict:
                self._cfg = self._config_dict
            elif self.config_path.exists():
                with open(self.config_path, encoding="UTF-8") as f:
                    self._cfg = yaml.safe_load(f)
            else:
                raise FileNotFoundError(
                    "KIS 설정이 없습니다. 대시보드 > 설정에서 계좌정보와 앱키를 입력하세요."
                )
        return self._cfg

    def _get_url(self) -> str:
        cfg = self._load_config()
        if self.env_dv == "demo":
            return cfg.get("vps", "https://openapivts.koreainvestment.com:29443")
        return cfg.get("prod", "https://openapi.koreainvestment.com:9443")

    def _need_token_refresh(self) -> bool:
        if self._token is None or self._token_expired is None:
            return True
        margin = datetime.now().timestamp() + 600
        return self._token_expired.timestamp() < margin

    def auth(self, ctac_tlno: str = "01000000000", max_retries: int = 3, force: bool = False) -> bool:
        if not force and not self._need_token_refresh():
            return True
        with self._auth_lock:
            if not force and not self._need_token_refresh():
                return True
            return self._do_auth(ctac_tlno, max_retries)

    def _load_token_from_file(self, app_key: str) -> bool:
        """캐시 파일에서 토큰 로드. 유효하면 사용 (API 호출 없음)"""
        path = _token_cache_path(self.env_dv, app_key)
        if not path.exists():
            return False
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            token = data.get("token") or data.get("access_token")
            exp_str = data.get("expired")
            if not token or not exp_str:
                return False
            naive = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
            expired = naive.replace(tzinfo=ZoneInfo("Asia/Seoul"))
            margin = datetime.now().timestamp() + 600
            if expired.timestamp() < margin:
                return False
            cfg = self._load_config()
            self._token = token
            self._token_expired = expired
            issued_str = data.get("issued")
            if issued_str:
                try:
                    self._token_issued_at = datetime.strptime(issued_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Seoul"))
                except Exception:
                    self._token_issued_at = None
            else:
                self._token_issued_at = None
            self._base_headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {self._token}",
                "appkey": app_key,
                "appsecret": cfg.get("paper_sec" if self.env_dv == "demo" else "my_sec", ""),
                "custtype": "P",
                "tr_cont": "",
                "charset": "UTF-8",
                "User-Agent": cfg.get("my_agent", "Mozilla/5.0"),
            }
            logger.info(f"토큰 캐시 로드 (만료: {exp_str})")
            return True
        except Exception as e:
            logger.debug(f"토큰 캐시 로드 실패: {e}")
            return False

    def _save_token_to_file(self, app_key: str) -> None:
        """발급된 토큰을 캐시 파일에 저장"""
        if not self._token or not self._token_expired:
            return
        path = _token_cache_path(self.env_dv, app_key)
        try:
            TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            exp_str = self._token_expired.strftime("%Y-%m-%d %H:%M:%S")
            issued_str = self._token_issued_at.strftime("%Y-%m-%d %H:%M:%S") if self._token_issued_at else None
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"token": self._token, "expired": exp_str, "issued": issued_str}, f, ensure_ascii=False)
            path.chmod(0o600)
        except Exception as e:
            logger.debug(f"토큰 캐시 저장 실패: {e}")

    def _do_auth(self, ctac_tlno: str, max_retries: int) -> bool:
        cfg = self._load_config()
        if self.env_dv == "demo":
            app_key = cfg.get("paper_app", "")
            app_secret = cfg.get("paper_sec", "")
        else:
            app_key = cfg.get("my_app", "")
            app_secret = cfg.get("my_sec", "")
        if not app_key or not app_secret:
            logger.error("앱키/시크릿이 설정되지 않았습니다.")
            return False
        if self._load_token_from_file(app_key):
            return True
        url = f"{self._get_url()}/oauth2/tokenP"
        data = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
        headers = {"Content-Type": "application/json", "Accept": "text/plain", "charset": "UTF-8",
                    "User-Agent": cfg.get("my_agent", "Mozilla/5.0")}
        for attempt in range(1, max_retries + 1):
            try:
                res = requests.post(url, headers=headers, json=data)
                if res.status_code == 200:
                    j = res.json()
                    self._token = j["access_token"]
                    exp_str = j["access_token_token_expired"]
                    naive = datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
                    self._token_expired = naive.replace(tzinfo=ZoneInfo("Asia/Seoul"))
                    self._token_issued_at = datetime.now(ZoneInfo("Asia/Seoul"))
                    self._base_headers = {
                        "Content-Type": "application/json", "authorization": f"Bearer {self._token}",
                        "appkey": app_key, "appsecret": app_secret, "custtype": "P",
                        "tr_cont": "", "charset": "UTF-8",
                        "User-Agent": cfg.get("my_agent", "Mozilla/5.0"),
                    }
                    self._save_token_to_file(app_key)
                    logger.info(f"토큰 발급 완료 (만료: {exp_str})")
                    return True
                if res.status_code == 403 and "EGW00133" in res.text:
                    if attempt < max_retries:
                        logger.warning(f"토큰 발급 제한 - 65초 대기 ({attempt}/{max_retries})")
                        time.sleep(65)
                        continue
                    logger.error(f"토큰 발급 실패: {max_retries}회 재시도 모두 실패")
                    return False
                logger.error(f"토큰 발급 실패: {res.status_code} {res.text}")
                return False
            except Exception as e:
                logger.exception(f"토큰 발급 오류 (시도 {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(10)
                    continue
                return False
        return False

    def _ensure_auth(self, ctac_tlno: str = "01000000000") -> bool:
        if self._need_token_refresh():
            return self.auth(ctac_tlno)
        return True

    def get_token_display_times(self) -> dict:
        """API 토큰 발급 시각·만료 시각 (KST 문자열). 대시보드 표시용."""
        out = {"issued_kst": None, "expired_kst": None}
        if self._token_expired:
            out["expired_kst"] = self._token_expired.strftime("%Y-%m-%d %H:%M:%S") + " KST"
        if self._token_issued_at:
            out["issued_kst"] = self._token_issued_at.strftime("%Y-%m-%d %H:%M:%S") + " KST"
        return out

    def _get_account(self) -> tuple[str, str]:
        cfg = self._load_config()
        if self.env_dv == "demo":
            acct = cfg.get("my_paper_stock", "")
        else:
            acct = cfg.get("my_acct_stock", cfg.get("my_acct", ""))
        prod = cfg.get("my_prod", "01")
        acct = (acct or "").replace("-", "").replace(" ", "")
        if len(acct) >= 10:
            return acct[:8], acct[-2:]
        return (acct[:8] if acct else "00000000").ljust(8, "0"), prod

    def _request(self, method: str, url_path: str, tr_id: str,
                 params: Optional[dict] = None, data: Optional[dict] = None,
                 ctac_tlno: str = "01000000000", _retried_401: bool = False) -> dict:
        if not self._ensure_auth(ctac_tlno):
            return {"rt_cd": "1", "msg_cd": "AUTH_FAIL", "msg1": "토큰 발급 실패"}
        url = f"{self._get_url()}{url_path}"
        headers = {**self._base_headers, "tr_id": tr_id}
        if self.env_dv == "demo" and tr_id.startswith("T"):
            headers["tr_id"] = "V" + tr_id[1:]
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, params=params or {})
            else:
                r = requests.post(url, headers=headers, json=data or params or {})
            if r.status_code == 401 and not _retried_401:
                self._token = None
                self._token_expired = None
                self._token_issued_at = None
                logger.warning("API 401(토큰 만료) - 재발급 후 1회 재시도")
                if self.auth(ctac_tlno, force=True):
                    return self._request(method, url_path, tr_id, params, data, ctac_tlno, _retried_401=True)
            body = r.json()
            body["tr_cont"] = r.headers.get("tr_cont", "")
            return body
        except Exception as e:
            logger.exception(f"API 요청 오류: {e}")
            return {"rt_cd": "1", "msg_cd": "REQ_ERR", "msg1": str(e)}

    def inquire_price(self, pdno: str, ovrs_excg_cd: str = "NASD",
                      ctac_tlno: str = "01000000000") -> float:
        """현재가/최종가. EXCD=3글자. SOXL(Arca)은 NYS 먼저."""
        tr_id = "HHDFS76200200"
        excd_first = _ovrs_to_excd(ovrs_excg_cd)
        excds = [excd_first] if excd_first not in _US_EXCD_FALLBACK else _US_EXCD_FALLBACK
        for excd in excds:
            res = self._request("GET", "/uapi/overseas-price/v1/quotations/price", tr_id,
                                params={"AUTH": "", "EXCD": excd, "SYMB": pdno}, ctac_tlno=ctac_tlno)
            if res.get("rt_cd") != "0":
                continue
            output = res.get("output", {})
            if isinstance(output, list):
                output = output[0] if output else {}
            last = output.get("last") or output.get("base") or output.get("stck_prpr") or "0"
            try:
                val = float(last)
                if val > 0:
                    return val
            except (ValueError, TypeError):
                pass
        return 0.0

    def inquire_prev_close(self, pdno: str, ovrs_excg_cd: str = "NASD",
                           ctac_tlno: str = "01000000000") -> tuple[float, str]:
        """전일종가. EXCD=3글자. SOXL(Arca)은 NYS 먼저."""
        tr_id = "HHDFS76200200"
        excd_first = _ovrs_to_excd(ovrs_excg_cd)
        excds = [excd_first] if excd_first not in _US_EXCD_FALLBACK else _US_EXCD_FALLBACK
        for excd in excds:
            res = self._request("GET", "/uapi/overseas-price/v1/quotations/price", tr_id,
                                params={"AUTH": "", "EXCD": excd, "SYMB": pdno}, ctac_tlno=ctac_tlno)
            if res.get("rt_cd") != "0":
                continue
            output = res.get("output", {})
            if isinstance(output, list):
                output = output[0] if output else {}
            base_val = output.get("base")
            last_val = output.get("last") or output.get("stck_prpr")
            try:
                base_f = float(base_val) if base_val is not None and str(base_val).strip() else 0.0
                last_f = float(last_val) if last_val is not None and str(last_val).strip() else 0.0
            except (ValueError, TypeError):
                continue
            if base_f > 0:
                return base_f, "base"
            if last_f > 0:
                return last_f, "last"
        return 0.0, "none"

    def inquire_balance(self, ovrs_excg_cd: str = "NASD", tr_crcy_cd: str = "USD",
                        ctac_tlno: str = "01000000000") -> tuple[pd.DataFrame, pd.DataFrame]:
        cano, acnt_prdt_cd = self._get_account()
        tr_id = "VTTS3012R" if self.env_dv == "demo" else "TTTS3012R"
        res = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-balance", tr_id,
                            params={"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                                    "OVRS_EXCG_CD": ovrs_excg_cd, "TR_CRCY_CD": tr_crcy_cd,
                                    "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
                            ctac_tlno=ctac_tlno)
        if res.get("rt_cd") != "0":
            logger.error(f"잔고 조회 실패: {res.get('msg1', '')}")
            return pd.DataFrame(), pd.DataFrame()
        out1 = res.get("output1")
        out2 = res.get("output2")
        df1 = pd.DataFrame([out1] if out1 and not isinstance(out1, list) else (out1 or []))
        df2 = pd.DataFrame([out2] if out2 and not isinstance(out2, list) else (out2 or []))
        return df1, df2

    def inquire_present_balance(self, natn_cd: str = "840", tr_mket_cd: str = "00",
                                ctac_tlno: str = "01000000000") -> dict:
        cano, acnt_prdt_cd = self._get_account()
        tr_id = "VTRP6504R" if self.env_dv == "demo" else "CTRP6504R"
        res = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-present-balance", tr_id,
                            params={"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                                    "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": natn_cd,
                                    "TR_MKET_CD": tr_mket_cd, "INQR_DVSN_CD": "00"},
                            ctac_tlno=ctac_tlno)
        result = {"tot_asst_krw": 0.0, "exrt": 0.0, "cash_usd": 0.0, "deposit_usd": 0.0}
        if res.get("rt_cd") != "0":
            return result
        def _float(d, *keys):
            for k in keys:
                v = d.get(k)
                if v is not None:
                    try: return float(v)
                    except: continue
            return 0.0
        output3 = res.get("output3", {})
        if isinstance(output3, list): output3 = output3[0] if output3 else {}
        result["tot_asst_krw"] = _float(output3, "tot_asst_amt")
        output2 = res.get("output2", [])
        if isinstance(output2, list):
            for item in output2:
                if item.get("crcy_cd") == "USD":
                    result["exrt"] = _float(item, "frst_bltn_exrt")
                    result["deposit_usd"] = _float(item, "frcr_dncl_amt_2")
                    result["cash_usd"] = _float(item, "frcr_drwg_psbl_amt_1")
                    break
        elif isinstance(output2, dict):
            result["exrt"] = _float(output2, "frst_bltn_exrt")
            result["deposit_usd"] = _float(output2, "frcr_dncl_amt_2")
            result["cash_usd"] = _float(output2, "frcr_drwg_psbl_amt_1")
        return result

    def inquire_ccnl(self, pdno: str, ord_strt_dt: str, ord_end_dt: str,
                     sll_buy_dvsn: str = "00", ccld_nccs_dvsn: str = "01",
                     ovrs_excg_cd: str = "%", ctac_tlno: str = "01000000000") -> pd.DataFrame:
        """
        해외주식 주문체결내역 조회 (연속조회로 전체 데이터 수집)
        API: /uapi/overseas-stock/v1/trading/inquire-ccnl
        참고: https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/overseas-stock/v1/trading/inquire-ccnl

        출력 필드 (공식 문서 기준): ccld_unpr/ft_ccld_unpr3(체결단가), ft_ccld_qty/ccld_qty(체결수량),
        tot_ccld_amt(총체결금액, 단 주문금액으로 반환되는 경우 있음), ord_unpr/ovrs_ord_unpr(주문단가)
        → 체결금액은 ccld_unpr × qty 로 계산 권장 (tot_ccld_amt 신뢰 시 주문금액으로 잘못 표시 가능)
        """
        cano, acnt_prdt_cd = self._get_account()
        tr_id = "VTTS3035R" if self.env_dv == "demo" else "TTTS3035R"
        all_rows = []
        ctx_fk200 = ctx_nk200 = ""
        for page in range(20):
            res = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-ccnl", tr_id,
                                params={"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                                        "PDNO": pdno, "ORD_STRT_DT": ord_strt_dt,
                                        "ORD_END_DT": ord_end_dt, "SLL_BUY_DVSN": sll_buy_dvsn,
                                        "CCLD_NCCS_DVSN": ccld_nccs_dvsn, "OVRS_EXCG_CD": ovrs_excg_cd,
                                        "SORT_SQN": "DS", "ORD_DT": "", "ORD_GNO_BRNO": "",
                                        "ODNO": "", "CTX_AREA_NK200": ctx_nk200,
                                        "CTX_AREA_FK200": ctx_fk200},
                                ctac_tlno=ctac_tlno)
            if res.get("rt_cd") != "0": break
            out = res.get("output", [])
            if not out: break
            rows = out if isinstance(out, list) else [out]
            all_rows.extend(rows)
            tr_cont = res.get("tr_cont", "")
            ctx_fk200 = res.get("ctx_area_fk200", "")
            ctx_nk200 = res.get("ctx_area_nk200", "")
            if tr_cont not in ("M", "F") or (not ctx_fk200 and not ctx_nk200): break
            time.sleep(0.5)
        return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

    def inquire_period_trans(self, inqr_strt_dt: str, inqr_end_dt: str,
                             pdno: str = "", sll_buy_dvsn: str = "00",
                             ovrs_excg_cd: str = "%", ctac_tlno: str = "01000000000") -> pd.DataFrame:
        """
        해외주식 일별거래내역 조회
        API: /uapi/overseas-stock/v1/trading/inquire-period-trans
        참고: https://apiportal.koreainvestment.com/apiservice-apiservice?/uapi/overseas-stock/v1/trading/inquire-period-trans

        inquire-ccnl(주문체결내역)과 함께 참고하여 체결가/체결금액 검증·보정용으로 활용 가능.
        TR_ID는 공식 문서에서 확인 (해외주식 일별거래내역 페이지).
        """
        cano, acnt_prdt_cd = self._get_account()
        tr_id = "VTTT3012R" if self.env_dv == "demo" else "JTTT3012R"
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                  "INQR_STRT_DT": inqr_strt_dt, "INQR_END_DT": inqr_end_dt,
                  "SLL_BUY_DVSN": sll_buy_dvsn, "OVRS_EXCG_CD": ovrs_excg_cd,
                  "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        if pdno:
            params["PDNO"] = pdno
        res = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-period-trans",
                            tr_id, params=params, ctac_tlno=ctac_tlno)
        if res.get("rt_cd") != "0":
            logger.warning(f"일별거래내역 조회 실패: {res.get('msg1', '')}")
            return pd.DataFrame()
        out = res.get("output", [])
        if not out:
            return pd.DataFrame()
        rows = out if isinstance(out, list) else [out]
        return pd.DataFrame(rows)

    def inquire_nccs(self, ovrs_excg_cd: str = "NASD", sort_sqn: str = "DS",
                     ctac_tlno: str = "01000000000") -> pd.DataFrame:
        cano, acnt_prdt_cd = self._get_account()
        tr_id = "VTTS3018R" if self.env_dv == "demo" else "TTTS3018R"
        res = self._request("GET", "/uapi/overseas-stock/v1/trading/inquire-nccs", tr_id,
                            params={"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd,
                                    "OVRS_EXCG_CD": ovrs_excg_cd, "SORT_SQN": sort_sqn,
                                    "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""},
                            ctac_tlno=ctac_tlno)
        if res.get("rt_cd") != "0": return pd.DataFrame()
        out = res.get("output", [])
        return pd.DataFrame(out if isinstance(out, list) else [out])

    def order(self, ord_dv: str, pdno: str, ord_qty: str, ovrs_ord_unpr: str,
              ord_dvsn: str = ORD_DVSN_LIMIT, ovrs_excg_cd: str = "NASD",
              ctac_tlno: str = "01000000000") -> dict:
        cano, acnt_prdt_cd = self._get_account()
        ovrs = _normalize_ovrs_excg(ovrs_excg_cd)
        if ord_dv == "buy":
            tr_id = "VTTT1002U" if self.env_dv == "demo" else "TTTT1002U"
        else:
            tr_id = "VTTT1006U" if self.env_dv == "demo" else "TTTT1006U"
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": ovrs,
                  "PDNO": pdno, "ORD_QTY": str(int(float(ord_qty))),
                  "OVRS_ORD_UNPR": str(round(float(ovrs_ord_unpr), 2)),
                  "CTAC_TLNO": ctac_tlno or "01000000000", "MGCO_APTM_ODNO": "",
                  "SLL_TYPE": "" if ord_dv == "buy" else "00",
                  "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": ord_dvsn}
        res = self._request("POST", "/uapi/overseas-stock/v1/trading/order", tr_id,
                            data=params, ctac_tlno=ctac_tlno)
        msg1 = (res.get("msg1") or "")
        if res.get("rt_cd") != "0" and "해당종목정보가 없습니다" in msg1:
            for retry_ovrs in (["NYSE", "NAS", "AMEX"] if ovrs == "NASD" else ["NASD", "NYSE", "NAS"]):
                if retry_ovrs == ovrs:
                    continue
                logger.warning(f"OVRS_EXCG_CD={ovrs} 실패 → {retry_ovrs}로 재시도")
                params["OVRS_EXCG_CD"] = retry_ovrs
                res = self._request("POST", "/uapi/overseas-stock/v1/trading/order", tr_id,
                                    data=params, ctac_tlno=ctac_tlno)
                if res.get("rt_cd") == "0":
                    return res
        return res

    def order_cancel(self, pdno: str, orgn_odno: str, ord_qty: str, ovrs_ord_unpr: str,
                     ovrs_excg_cd: str = "NASD", ctac_tlno: str = "01000000000") -> dict:
        cano, acnt_prdt_cd = self._get_account()
        ovrs = _normalize_ovrs_excg(ovrs_excg_cd)
        tr_id = "VTTT1004U" if self.env_dv == "demo" else "TTTT1004U"
        params = {"CANO": cano, "ACNT_PRDT_CD": acnt_prdt_cd, "OVRS_EXCG_CD": ovrs,
                  "PDNO": pdno, "ORGN_ODNO": orgn_odno, "RVSE_CNCL_DVSN_CD": "02",
                  "ORD_QTY": ord_qty, "OVRS_ORD_UNPR": ovrs_ord_unpr or "0",
                  "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0"}
        return self._request("POST", "/uapi/overseas-stock/v1/trading/order-rvsecncl", tr_id,
                             data=params, ctac_tlno=ctac_tlno)
