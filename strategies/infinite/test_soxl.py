# -*- coding: utf-8 -*-
"""
SOXL (Direxion Daily Semiconductor Bull 3X) 시세/가격 조회 테스트
- KIS API: EXCD NYS/NAS/AMS 순서로 폴백 (NYSE Arca = NYS)
- KIS 실패 시 yfinance 폴백
"""
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

TICKER = "SOXL"


def _yfinance_price(ticker: str) -> float:
    """yfinance로 가격 조회. 실패 시 0.0"""
    try:
        import yfinance as yf
    except ImportError:
        print("     ※ yfinance 미설치. 다음으로 설치: pip install yfinance")
        return 0.0
    try:
        t = yf.Ticker(ticker)
        info = t.info
        p = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
        if p is not None:
            fp = float(p)
            return fp if fp > 0 else 0.0
    except Exception as e:
        print(f"     ✗ yfinance 오류: {e}")
    return 0.0


def main():
    print("=" * 50)
    print(f"  {TICKER} (Direxion Daily Semiconductor Bull 3X) 테스트")
    print("  상장: NYSE Arca → EXCD=NYS 사용")
    print("=" * 50)

    price = 0.0
    prev_close = 0.0

    # KIS 설정 로드 (워커와 동일: DB 우선 → yaml 폴백)
    cfg = None
    use_yaml = False
    try:
        from .config import DATABASE_URL, KIS_DEVL_YAML
        from .settings_store import get_kis_settings

        cfg = get_kis_settings(DATABASE_URL)
        if cfg and (cfg.get("my_app") or cfg.get("paper_app")):
            print("\n[설정] DB에서 KIS 설정 로드")
        elif KIS_DEVL_YAML.exists():
            use_yaml = True
            print("\n[설정] yaml 파일 사용")
    except Exception as e:
        logger.debug(f"설정 로드: {e}")

    # 1. KIS 시세 조회 (설정 있으면)
    if (cfg and (cfg.get("my_app") or cfg.get("paper_app"))) or use_yaml:
        try:
            from .kis_client import get_shared_client
            from .config import KIS_DEVL_YAML

            if cfg and (cfg.get("my_app") or cfg.get("paper_app")):
                trading_mode = cfg.get("trading_mode", "demo")
                client = get_shared_client(config_dict=cfg, env_dv=trading_mode)
            else:
                client = get_shared_client(config_path=KIS_DEVL_YAML, env_dv="real")

            if client.auth():
                print("\n[1] KIS 현재가 조회 (EXCD: NYS→NAS→AMS)...")
                price = client.inquire_price(pdno=TICKER, ovrs_excg_cd="NASD")
                if price > 0:
                    print(f"     ✓ KIS 현재가: ${price:.2f}")
                else:
                    print("     ✗ KIS 현재가 실패")

                print("\n[2] KIS 전일종가(기준가) 조회...")
                prev_close, src = client.inquire_prev_close(pdno=TICKER, ovrs_excg_cd="NASD")
                if prev_close > 0:
                    print(f"     ✓ 전일종가: ${prev_close:.2f} (출처: {src})")
                    if price <= 0:
                        price = prev_close
                else:
                    print("     ✗ 전일종가 실패")
            else:
                print("\n[실패] KIS 인증 실패")
        except FileNotFoundError as e:
            print(f"\n[건너뜀] KIS 설정 없음: {e}")
        except Exception as e:
            logger.exception("KIS 클라이언트 오류")
            print(f"\n[오류] {e}")
    else:
        print("\n[건너뜀] KIS 설정 없음 → yfinance만 시도")

    # 2. yfinance 폴백
    if price <= 0:
        print("\n[3] yfinance 폴백...")
        price = _yfinance_price(TICKER)
        if price > 0:
            print(f"     ✓ Yahoo 가격: ${price:.2f}")

    # 결과
    print("\n" + "=" * 50)
    if price > 0:
        print(f"  결과: {TICKER} 가격 조회 성공 → ${price:.2f}")
        print("  → 최초매수 시 이 가격을 기준으로 주문 가능")
        return 0
    else:
        print("  결과: 가격 조회 실패 (KIS + Yahoo 모두 실패)")
        print("  ※ KIS: 대시보드 > 설정에서 계좌/앱키 입력 또는")
        print("         %USERPROFILE%\\KIS\\config\\kis_devlp.yaml 생성")
        print("  ※ Yahoo: pip install yfinance")
        return 1


if __name__ == "__main__":
    sys.exit(main())
