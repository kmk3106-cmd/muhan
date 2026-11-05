# -*- coding: utf-8 -*-
"""일일 점검 — 직전 미국 세션의 전략별 매수·매도를 '왜 됐는지/왜 안 됐는지'까지 정리.

매일 07:00 KST(미국장 마감 06:00 직후)에 스케줄러가 run_and_send() 를 부른다.

판정 근거는 추측하지 않는다. 세 가지 실데이터만 쓴다.
  1) 주문원장  : 그날 실제로 제출된 주문과 상태 (trade_orders / orders)
  2) 체결원장  : 실제 체결 (trades)
  3) 종가      : yfinance 일봉의 완료된 바 (LOC 체결가와 일치함을 2026-11-05 검증)
미체결이면 '한도 vs 종가'로 규칙대로인지 판정하고, 주문 자체가 없으면
app_logs 의 스킵/캡/실패 메시지에서 사유를 찾아 붙인다.

날짜 규약 (2026-11-05 실데이터로 확인):
  - 워커는 KST 00:15~05:25 에 도는데 이때는 **전날 미국 세션이 진행 중**이다.
  - 그래서 trade_orders.order_date(KST) = 미국 세션일 + 1
  - trades.trade_date = 미국 세션일
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger("daily_review")

_BASE = Path(__file__).resolve().parent.parent
_STATE = Path("/root/trading_suite_state")

#: (표시명, DB 경로) — 떨사오팔·종사종팔은 스키마가 같아 한 함수로 처리
_TRANCHE_DBS = [
    ("떨사오팔", _BASE / "strategies" / "ddsop" / "ddsop.db"),
    ("종사종팔", _BASE / "strategies" / "jongsa" / "jongsa.db"),
]
_INFINITE_DB = _BASE / "strategies" / "infinite" / "infinite_buy.db"

#: 메일에 "코드 점검 필요" 로 올릴 패턴 — 규칙대로 안 된 것이 아니라 시스템이 틀린 것
_SUSPECT_PATTERNS = [
    (r"Yahoo", "KIS 가격조회 실패 → Yahoo 폴백 사용 (전일종가 정확도 확인 필요)"),
    (r"현재가 API: 실패|가격 조회 실패", "가격 조회 실패로 주문 전체 스킵"),
    (r"APBK1943|자전거래 거부", "KIS 자전거래 거부 — 가드가 못 막은 것"),
    (r"초당 거래건수", "KIS 호출 속도 초과로 주문 거부"),
    (r"APBK0988|매도가능수량", "매도가능수량 초과 거부"),
    (r"수량 불일치|불일치 경보", "장부와 실보유 수량 불일치"),
]


def _conn(p) -> sqlite3.Connection | None:
    try:
        if not Path(p).exists():
            return None
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c
    except Exception as e:
        logger.warning(f"[daily_review] DB 열기 실패 {p}: {e}")
        return None


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


#: 같은 자리의 주문이 시간당 재제출되며 여러 행으로 남는다 (구행은 cancelled).
#: 자리별로 '최종 상태' 한 건만 남겨야 '한도 이하인데 미체결' 같은 오탐이 안 난다.
#: 무한매수법은 'success'(주문 전송 완료)를 쓴다 — 체결이 아니라 '제출됨' 이라서
#: 체결 판정은 반드시 trades 의 odno 와 맞춰야 한다 (2026-11-05 오탐으로 확인).
_ST_RANK = {"filled": 5, "pending": 3, "submitted": 3, "success": 3, "failed": 2, "cancelled": 1}


def _dedup_final(orders: list[dict], keyfn) -> list[dict]:
    best: dict = {}
    for o in orders:
        k = keyfn(o)
        cur = best.get(k)
        if cur is None or _ST_RANK.get(o.get("status"), 0) >= _ST_RANK.get(cur.get("status"), 0):
            best[k] = o
    return list(best.values())


def _next_kst(sess: str) -> str:
    return _ymd(dt.datetime.strptime(sess, "%Y%m%d").date() + dt.timedelta(days=1))


# ───────────────────────── 종가 (yfinance 완료 일봉) ─────────────────────────

def _closes(tickers: list[str], want_sess: str | None) -> tuple[dict, str | None]:
    """{ticker: 종가} 와 기준 세션일. want_sess=None 이면 가장 최근 완료 세션."""
    out: dict[str, float] = {}
    sess = want_sess
    try:
        import yfinance as yf
    except Exception as e:
        logger.warning(f"[daily_review] yfinance 없음: {e}")
        return out, sess
    for tk in tickers:
        try:
            h = yf.Ticker(tk).history(period="10d", interval="1d", auto_adjust=False)
            if h is None or h.empty:
                continue
            rows = [(i.strftime("%Y%m%d"), float(v)) for i, v in h["Close"].items()
                    if v == v and float(v) > 0]
            if not rows:
                continue
            if sess is None:
                sess = rows[-1][0]
            m = {d: c for d, c in rows}
            if sess in m:
                out[tk] = m[sess]
        except Exception as e:
            logger.warning(f"[daily_review] {tk} 일봉 실패: {e}")
    return out, sess


# ───────────────────────── 트렌치형 (떨사오팔·종사종팔) ─────────────────────────

def _logs(con, sess: str, like: str) -> list[str]:
    """해당 세션(KST 기준 당일+다음날 05시까지)의 로그 메시지."""
    kst_next = _next_kst(sess)
    lo = dt.datetime.strptime(sess, "%Y%m%d") + dt.timedelta(hours=14 - 9)   # UTC 저장
    hi = dt.datetime.strptime(kst_next, "%Y%m%d") + dt.timedelta(hours=21)
    try:
        return [dict(r)["message"] for r in con.execute(
            "SELECT message FROM app_logs WHERE message LIKE ? AND created_at>=? AND created_at<=? "
            "ORDER BY id", (like, lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")))]
    except Exception:
        return []


def _tranche_section(name: str, db, sess: str) -> dict:
    con = _conn(db)
    if con is None:
        return {"strategy": name, "error": "DB 없음", "tickers": []}
    try:
        tks = [dict(r) for r in con.execute(
            "SELECT id,ticker,num_tranches,x_pct,loss_cut_days,current_cycle,trading_enabled FROM tickers")]
        closes, _ = _closes([t["ticker"] for t in tks], sess)
        out = []
        for t in tks:
            tk = t["ticker"]
            close = closes.get(tk)
            tid = {r["id"]: r["tranche_num"] for r in con.execute(
                "SELECT id,tranche_num FROM tranches WHERE ticker_id=?", (t["id"],))}
            # 그 세션용으로 제출된 주문 = order_date 가 세션+1 (KST)
            orders, seen = [], set()
            for r in con.execute(
                    "SELECT side,order_type,price,qty,status,tranche_id FROM trade_orders "
                    "WHERE ticker=? AND order_date=? ORDER BY id", (tk, _next_kst(sess))):
                d = dict(r)
                key = (d["side"], d["order_type"], round(d["price"] or 0, 2), d["tranche_id"])
                if key in seen:
                    continue
                seen.add(key)
                d["T"] = tid.get(d["tranche_id"], "?")
                orders.append(d)
            fills = [dict(r) for r in con.execute(
                "SELECT tranche_num,side,order_type,price,qty,amount FROM trades "
                "WHERE ticker=? AND trade_date=? ORDER BY id", (tk, sess))]
            filled_keys = {(f["tranche_num"], f["side"]) for f in fills}
            suspect_extra: list[str] = []

            orders = _dedup_final(orders, lambda o: (o["side"], o["order_type"], o["T"]))
            rows = []
            for o in orders:
                done = (o["T"], o["side"]) in filled_keys or o["status"] == "filled"
                if done:
                    fp = next((f["price"] for f in fills
                               if f["tranche_num"] == o["T"] and f["side"] == o["side"]), None)
                    why = f"${fp:.2f} 에 체결" if fp else "체결"
                elif o["status"] == "cancelled":
                    why = "취소 — 다음 워커가 갱신가로 재제출 (정상)"
                elif o["status"] in ("failed",):
                    why = "주문 거부 (아래 오류 참고)"
                elif o["order_type"] == "MOC":
                    why = "MOC 미체결 — 확인 필요"
                elif close is None:
                    why = "종가 미확정"
                elif o["side"] == "buy":
                    why = (f"종가 ${close:.2f} > 한도 ${o['price']:.2f} → 규칙대로 미체결"
                           if close > o["price"] else "한도 이하인데 미체결 — 확인 필요")
                else:
                    why = (f"종가 ${close:.2f} < 한도 ${o['price']:.2f} → 규칙대로 미체결"
                           if close < o["price"] else "한도 이상인데 미체결 — 확인 필요")
                rows.append({"T": o["T"], "side": "매수" if o["side"] == "buy" else "매도",
                             "type": o["order_type"], "price": o["price"], "qty": o["qty"],
                             "done": done, "why": why})

            notes = []
            if not any(o["side"] == "buy" for o in orders):
                # 사유는 DB 사실에서 먼저 찾는다. 종사종팔의 '손절일 매수금지' 로그는
                # _log_structured 가 아니라 평문 logger 로 남아 app_logs 에 없다.
                reason = None
                if any(o["side"] == "sell" and o["order_type"] == "MOC" for o in orders):
                    reason = "손절 MOC 가 있는 날이라 매수 금지 (종사종팔4 규칙)"
                else:
                    st = [dict(r)["status"] for r in con.execute(
                        "SELECT status FROM tranches WHERE ticker_id=?", (t["id"],))]
                    if st and all(x == "BOUGHT" for x in st):
                        reason = f"빈 트렌치 없음 (전 {len(st)}칸 보유중)"
                if reason is None:
                    sk = [m for m in _logs(con, sess, f"%{tk}%")
                          if ("[스킵]" in m or "매수금지" in m or "자전거래" in m or "예수금" in m)]
                    reason = (re.sub(r"^\[[\d:]+\]\s*", "", sk[-1]) if sk
                              else "사유 확인 필요 — 주문도 스킵 로그도 없음")
                notes.append("매수 주문 없음 — " + reason)
                if "확인 필요" in reason:
                    suspect_extra.append(reason)
            cap = [m for m in _logs(con, sess, f"%{tk}%") if "[캡]" in m]
            if cap:
                notes.append(re.sub(r"^\[[\d:]+\]\s*", "", cap[-1]))

            errs = [re.sub(r"^\[[\d:]+\]\s*", "", m) for m in _logs(con, sess, f"%{tk}%")
                    if "주문실패" in m or "거부" in m]
            suspects = []
            for m in _logs(con, sess, f"%{tk}%"):
                for pat, label in _SUSPECT_PATTERNS:
                    if re.search(pat, m):
                        suspects.append(label)
            out.append({"ticker": tk, "close": close, "cycle": t["current_cycle"],
                        "x_pct": t["x_pct"], "enabled": bool(t["trading_enabled"]),
                        "orders": rows, "fills": fills, "notes": notes,
                        "errors": sorted(set(errs))[:5],
                        "suspects": sorted(set(suspects + suspect_extra))})
        return {"strategy": name, "tickers": out}
    finally:
        con.close()


# ───────────────────────── 무한매수법 ─────────────────────────

def _infinite_section(sess: str) -> dict:
    con = _conn(_INFINITE_DB)
    if con is None:
        return {"strategy": "무한매수법", "error": "DB 없음", "tickers": []}
    try:
        ports = [dict(r) for r in con.execute(
            "SELECT id,ticker,A,R,trading_enabled FROM portfolios WHERE is_active=1")]
        closes, _ = _closes([p["ticker"] for p in ports], sess)
        out = []
        for p in ports:
            tk = p["ticker"]
            close = closes.get(tk)
            orders = [dict(r) for r in con.execute(
                "SELECT side,order_type,price,qty,status,msg,odno FROM orders "
                "WHERE portfolio_id=? AND order_date=? ORDER BY id", (p["id"], _next_kst(sess)))]
            fills = [dict(r) for r in con.execute(
                "SELECT side,order_type,price,qty,amount,buy_seq,odno FROM trades "
                "WHERE portfolio_id=? AND trade_date=? ORDER BY id", (p["id"], sess))]
            # 체결 판정: 주문번호(odno) 일치가 정답. 없으면 (구분, 주문종류)로 보조 매칭.
            fodno = {f["odno"] for f in fills if f.get("odno")}
            fpair = {(f["side"], f["order_type"]) for f in fills}
            orders = _dedup_final(orders, lambda o: (o["side"], o["order_type"]))
            rows = []
            for o in orders:
                done = (o.get("odno") in fodno) if fodno else ((o["side"], o["order_type"]) in fpair)
                if done:
                    fp = next((f["price"] for f in fills if f.get("odno") == o.get("odno")), None)
                    why = f"${fp:.2f} 에 체결" if fp else "체결"
                elif o["status"] == "cancelled":
                    why = "취소 — 다음 워커가 갱신가로 재제출 (정상)"
                elif o["status"] == "failed":
                    why = f"주문 거부 — {(o['msg'] or '')[:60]}"
                elif o["order_type"] == "MOC":
                    why = "MOC 미체결 — 확인 필요"
                elif close is None:
                    why = "종가 미확정"
                elif o["side"] == "buy":
                    why = (f"종가 ${close:.2f} > 한도 ${o['price']:.2f} → 규칙대로 미체결"
                           if close > (o["price"] or 0) else "한도 이하인데 미체결 — 확인 필요")
                else:
                    why = (f"종가 ${close:.2f} < 한도 ${o['price']:.2f} → 규칙대로 미체결"
                           if close < (o["price"] or 0) else "한도 이상인데 미체결 — 확인 필요")
                rows.append({"T": "-", "side": "매수" if o["side"] == "buy" else "매도",
                             "type": o["order_type"], "price": o["price"], "qty": o["qty"],
                             "done": done, "why": why})
            msgs = []
            try:
                kst_next = _next_kst(sess)
                lo = dt.datetime.strptime(sess, "%Y%m%d") + dt.timedelta(hours=5)
                hi = dt.datetime.strptime(kst_next, "%Y%m%d") + dt.timedelta(hours=21)
                msgs = [dict(r)["message"] for r in con.execute(
                    "SELECT message FROM app_logs WHERE portfolio_id=? AND created_at>=? AND created_at<=?",
                    (p["id"], lo.strftime("%Y-%m-%d %H:%M:%S"), hi.strftime("%Y-%m-%d %H:%M:%S")))]
            except Exception:
                pass
            suspects = sorted({lab for m in msgs for pat, lab in _SUSPECT_PATTERNS if re.search(pat, m)})
            errs = sorted({re.sub(r"^\[[\d:]+\]\s*", "", m) for m in msgs
                           if "주문실패" in m or "거부" in m})[:5]
            notes = [] if orders else ["주문 없음 — 스케줄 미도달 또는 조건 미충족"]
            out.append({"ticker": tk, "close": close, "cycle": None, "x_pct": None,
                        "enabled": bool(p["trading_enabled"]), "orders": rows,
                        "fills": fills, "notes": notes, "errors": errs, "suspects": suspects})
        return {"strategy": "무한매수법", "tickers": out}
    finally:
        con.close()


# ───────────────────────── 리포트 조립 ─────────────────────────

def build_report(sess: str | None = None) -> dict:
    if sess is None:
        _, sess = _closes(["SPY"], None)
    if sess is None:
        sess = _ymd(dt.date.today() - dt.timedelta(days=1))
    sections = [_infinite_section(sess)]
    for name, db in _TRANCHE_DBS:
        sections.append(_tranche_section(name, db, sess))
    suspects = []
    for s in sections:
        for t in s.get("tickers", []):
            for sp in t.get("suspects", []):
                suspects.append(f"[{s['strategy']} {t['ticker']}] {sp}")
            for r in t.get("orders", []):
                if "확인 필요" in r["why"]:
                    suspects.append(f"[{s['strategy']} {t['ticker']}] "
                                    f"T{r['T']} {r['side']} {r['why']}")
    return {"session": sess, "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sections": sections, "suspects": sorted(set(suspects))}


def render_html(rep: dict) -> str:
    s = rep["session"]
    d = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    css = ("body{font-family:-apple-system,'Malgun Gothic',sans-serif;font-size:14px;color:#222}"
           "table{border-collapse:collapse;width:100%;margin:6px 0 16px}"
           "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left}"
           "th{background:#f4f6f8}.ok{color:#0a7d33;font-weight:600}.no{color:#888}"
           "h2{margin:22px 0 4px;font-size:16px;border-bottom:2px solid #333;padding-bottom:4px}"
           "h3{margin:14px 0 2px;font-size:14px}.warn{background:#fff4e5;border-left:4px solid #e8900c;"
           "padding:10px 12px;margin:10px 0}.err{color:#c0392b}.muted{color:#777;font-size:12px}")
    h = [f"<html><head><meta charset='utf-8'><style>{css}</style></head><body>",
         f"<h1 style='font-size:18px'>자동매매 일일 점검 — {d} 미국 세션</h1>",
         f"<div class='muted'>생성 {rep['generated_at']} KST</div>"]
    if rep["suspects"]:
        h.append("<div class='warn'><b>코드 점검 필요 " + str(len(rep["suspects"])) + "건</b><ul>")
        h += [f"<li>{x}</li>" for x in rep["suspects"]]
        h.append("</ul></div>")
    else:
        h.append("<div class='warn' style='background:#eef9f0;border-color:#0a7d33'>"
                 "<b>이상 없음</b> — 모든 주문이 규칙대로 처리됐습니다.</div>")
    for sec in rep["sections"]:
        h.append(f"<h2>{sec['strategy']}</h2>")
        if sec.get("error"):
            h.append(f"<div class='err'>{sec['error']}</div>")
            continue
        for t in sec["tickers"]:
            cl = f"${t['close']:.2f}" if t.get("close") else "—"
            extra = f" · 싸이클 {t['cycle']}" if t.get("cycle") else ""
            extra += f" · x={t['x_pct']}%" if t.get("x_pct") else ""
            h.append(f"<h3>{t['ticker']} <span class='muted'>종가 {cl}{extra}"
                     f"{'' if t['enabled'] else ' · 매매정지'}</span></h3>")
            if t["orders"]:
                h.append("<table><tr><th>트렌치</th><th>구분</th><th>종류</th><th>가격</th>"
                         "<th>수량</th><th>결과</th></tr>")
                for r in t["orders"]:
                    mark = ("<span class='ok'>체결</span>" if r["done"]
                            else "<span class='no'>미체결</span> —")
                    px = f"${r['price']:.2f}" if r["price"] else "시장가"
                    h.append(f"<tr><td>T{r['T']}</td><td>{r['side']}</td><td>{r['type']}</td>"
                             f"<td>{px}</td><td>{r['qty']}</td><td>{mark} {r['why']}</td></tr>")
                h.append("</table>")
            else:
                h.append("<div class='muted'>제출된 주문 없음</div>")
            for n in t["notes"]:
                h.append(f"<div class='muted'>· {n}</div>")
            for e in t["errors"]:
                h.append(f"<div class='err'>· {e}</div>")
    h.append("</body></html>")
    return "".join(h)


# ───────────────────────── 메일 ─────────────────────────

def _mail_cfg() -> dict:
    """/root/trading_suite_state/mail.env (앱 폴더 밖 — 배포해도 안 지워짐)."""
    cfg = {}
    f = _STATE / "mail.env"
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    for k in ("MAIL_FROM", "MAIL_APP_PASSWORD", "MAIL_TO"):
        cfg.setdefault(k, os.environ.get(k, ""))
    return cfg


def send_mail(subject: str, html: str) -> tuple[bool, str]:
    cfg = _mail_cfg()
    if not (cfg.get("MAIL_FROM") and cfg.get("MAIL_APP_PASSWORD") and cfg.get("MAIL_TO")):
        return False, "mail.env 미설정 (MAIL_FROM / MAIL_APP_PASSWORD / MAIL_TO)"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = cfg["MAIL_TO"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(cfg["MAIL_FROM"], cfg["MAIL_APP_PASSWORD"])
            s.sendmail(cfg["MAIL_FROM"], [x.strip() for x in cfg["MAIL_TO"].split(",")],
                       msg.as_string())
        return True, "발송 완료"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def run_and_send() -> dict:
    """스케줄러 엔트리 — 07:00 KST."""
    try:
        rep = build_report()
        n = len(rep["suspects"])
        s = rep["session"]
        subject = (f"[자동매매] {s[:4]}-{s[4:6]}-{s[6:]} 일일점검 — "
                   + (f"점검 필요 {n}건" if n else "이상 없음"))
        ok, detail = send_mail(subject, render_html(rep))
        logger.info(f"[daily_review] {subject} / 메일 {detail}")
        return {"ok": ok, "detail": detail, "session": s, "suspects": n}
    except Exception as e:
        logger.exception(f"[daily_review] 실패: {e}")
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
