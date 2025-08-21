# kiwoom_sac_client_bot.py
# ----------------------------------------
# 요구:
#  - Windows + 32bit Python (권장)
#  - KHOpenAPI+ 설치/로그인 가능
#  - pip install PyQt5 requests
# 기능:
#  - 초기 1분봉(TR) 로딩 → 실시간 틱 집계로 분봉 확정
#  - 최근 WINDOW개 OHLCV를 64bit 추론서버(/predict)에 POST
#  - target_w(목표 비중)에 맞춰 리밸런싱 주문(SendOrder)
#  - 리스크: 쿨다운(5초, 그대로), 최대 포지션 가치, 장마감 강제청산, 최소 리밸런싱 폭, 블록 단위 주문
#  - CSV 로그 저장
# ----------------------------------------

import sys, os, time, csv, collections
from datetime import datetime
import requests

from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer

# =========================
# 설정
# =========================
STOCK_CODE      = "005930"       # 예: 삼성전자
ACCOUNT_NO      = None           # None이면 로그인 후 첫 계좌
HOGA_MARKET     = "03"           # "03" 시장가, "00" 지정가
ORDER_SCREEN    = "9000"
REAL_SCREEN     = "3000"
TR_SCREEN       = "1000"

# 64bit 추론 서버
PREDICT_URL     = "http://127.0.0.1:8000/predict"
WINDOW          = 12

# 리스크/비용 파라미터
FEE_RATE        = 0.0015         # 수수료/세금(가정)
SLIPPAGE        = 0.0005
INITIAL_CASH    = 10_000_000
MAX_POSITION_VALUE   = 15_000_000   # 최대 주식 보유 가치
ORDER_COOLDOWN_SEC   = 1            # ⬅ 주문 빈도(그대로 5초->1초)
FORCE_LIQUIDATE_TIME = (15, 19)      # 15:19 강제청산

MIN_TRADE_QTY        = 1             # 최소 주문 수량

# 🔧 주문을 작게 쪼개지 않도록 하는 파라미터(신규/강화)
MIN_REBALANCE_RATIO  = 0.03          # 목표/현재 비중 차 3% 미만이면 스킵(기존 0.01 → 0.03)
TRADE_BLOCK_QTY      = 10            # 최소 10주 단위로만 매매
MIN_ORDER_VALUE_KRW  = 300_000       # 최소 30만원 어치 이상일 때만 매매
AGGRESSION_GAIN      = 1.5           # 목표비중으로 이동 가속(1.0=기본, 1.5~2.0 더 공격적)

ALLOW_LIVE_TRADING   = False         # 실계좌 보호 (모의 충분 검증 후 True)

SEED_BARS       = max(200, WINDOW + 5)  # 초기 분봉 로드 개수
LOG_PATH        = "trade_log.csv"

# =========================
# 유틸
# =========================
def now_kst():
    return datetime.now()  # 시스템 KST 가정

def same_minute(dt1, dt2):
    return (dt1.year, dt1.month, dt1.day, dt1.hour, dt1.minute) == \
           (dt2.year, dt2.month, dt2.day, dt2.hour, dt2.minute)

def to_int_safe(x):
    try:
        return int(str(x).strip())
    except:
        return 0

# =========================
# 메인 클래스
# =========================
class KiwoomSACClientBot(QAxWidget):
    def __init__(self):
        super().__init__("KHOPENAPI.KHOpenAPICtrl.1")

        # 이벤트 핸들
        self.OnEventConnect.connect(self._on_event_connect)
        self.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.OnReceiveRealData.connect(self._on_receive_real_data)
        self.OnReceiveChejanData.connect(self._on_chejan)

        # 루프
        self.login_loop = QEventLoop()
        self.tr_loop = QEventLoop()

        # 계좌/상태
        self.connected = False
        self.account = None

        self.cash = float(INITIAL_CASH)
        self.position = 0
        self.avg_buy_price = 0.0
        self.last_price = 0.0

        self.last_order_time = 0.0
        self.last_target_w = 0.0

        # 분봉 집계 버퍼
        self.cur_bar_minute = None
        self.cur_open = None
        self.cur_high = None
        self.cur_low = None
        self.cur_close = None
        self.cur_vol = 0

        self.bars = []  # 확정 분봉
        self.raw_buf = collections.deque(maxlen=WINDOW)  # 최근 WINDOW개 원시 OHLCV

        # 타이머(장마감 청산 등)
        self.timer = QTimer()
        self.timer.timeout.connect(self._time_tick)
        self.timer.start(1000)

    # ---------------- 로그인 ----------------
    def login(self):
        print("🔐 로그인 시도...")
        self.dynamicCall("CommConnect()")
        self.login_loop.exec_()

        if not self.connected:
            print("❌ 로그인 실패")
            sys.exit(1)

        # 계좌 선택
        raw_accounts = self.dynamicCall('GetLoginInfo(QString)', "ACCNO")
        accounts = [a for a in raw_accounts.split(';') if a]
        if not accounts:
            print("❌ 계좌 조회 실패")
            sys.exit(1)
        self.account = ACCOUNT_NO or accounts[0]
        print("📒 사용 계좌:", self.account)

        if not ALLOW_LIVE_TRADING:
            print("🧪 실계좌 보호: ALLOW_LIVE_TRADING=False (모의 권장)")

    def _on_event_connect(self, err_code):
        self.connected = (err_code == 0)
        print("✅ 로그인 성공" if self.connected else f"❌ 로그인 에러: {err_code}")
        self.login_loop.exit()

    # ---------------- 초기 분봉(TR) 로드 ----------------
    def load_seed_bars(self, code=STOCK_CODE, minute_unit=1, count=SEED_BARS):
        """
        opt10080: 주식분봉차트조회요청
        입력: 종목코드, 틱범위(분단위), 수정주가구분
        출력: 체결시간, 시가, 고가, 저가, 현재가, 거래량 ...
        """
        print("⏳ 초기 분봉 로딩(TR) ...")
        self.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.dynamicCall("SetInputValue(QString, QString)", "틱범위", str(minute_unit))
        self.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self.bar_accum = []
        self.dynamicCall("CommRqData(QString, QString, int, QString)",
                         "분봉요청", "opt10080", 0, TR_SCREEN)
        self.tr_loop.exec_()

        bars = self.bar_accum[:count]
        bars.reverse()  # 오래된→최신
        if len(bars) < WINDOW:
            print("❌ 초기 캔들 부족:", len(bars))
            sys.exit(1)

        # raw_buf 채우기
        for b in bars[-WINDOW:]:
            self.raw_buf.append([b["open"], b["high"], b["low"], b["close"], b["volume"]])

        # 현재 진행중 캔들 초기화
        last = bars[-1]
        self.cur_bar_minute = last["time"].replace(second=0, microsecond=0)
        self.cur_open  = last["open"]
        self.cur_high  = last["high"]
        self.cur_low   = last["low"]
        self.cur_close = last["close"]
        self.cur_vol   = last["volume"]
        self.last_price = last["close"]
        self.bars = bars
        print(f"✅ 초기 분봉 로드 완료: {len(bars)}개")

    def _on_receive_tr_data(self, screen_no, rqname, trcode, recordname, prev_next, *args):
        if rqname != "분봉요청":
            return
        cnt = self.dynamicCall("GetRepeatCnt(QString, QString)", trcode, rqname)
        for i in range(cnt):
            dt_str = self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "체결시간").strip()
            o = to_int_safe(self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "시가"))
            h = to_int_safe(self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "고가"))
            l = to_int_safe(self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "저가"))
            c = abs(to_int_safe(self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "현재가")))
            v = to_int_safe(self.dynamicCall("GetCommData(QString, QString, int, QString)", trcode, rqname, i, "거래량"))
            try:
                t = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
            except:
                continue
            self.bar_accum.append({"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
        self.tr_loop.exit()

    # ---------------- 실시간 등록 ----------------
    def start_realtime(self, code=STOCK_CODE):
        fids = "10;15;14;16"  # 현재가, 체결량(틱), 고가, 시가
        self.dynamicCall("SetRealReg(QString, QString, QString, QString)",
                         REAL_SCREEN, code, fids, "1")
        print(f"📡 실시간 등록 완료: {code} (FIDs={fids})")

    # ---------------- 틱 처리(분봉 집계) ----------------
    def _on_receive_real_data(self, code, real_type, real_data):
        if code != STOCK_CODE or real_type != "주식체결":
            return

        price_str = self.dynamicCall("GetCommRealData(QString, int)", code, 10)
        vol_str   = self.dynamicCall("GetCommRealData(QString, int)", code, 15)  # 체결량(틱)
        price = abs(to_int_safe(price_str))
        vol_tick = abs(to_int_safe(vol_str))

        if price <= 0:
            return

        self.last_price = price
        now_min = now_kst().replace(second=0, microsecond=0)

        if self.cur_bar_minute is None:
            self._start_new_bar(now_min, price, vol_tick)
            return

        if same_minute(now_min, self.cur_bar_minute):
            # 현재 분 진행 중
            if self.cur_open is None:
                self.cur_open = price
            self.cur_high = max(self.cur_high, price) if self.cur_high else price
            self.cur_low  = min(self.cur_low, price)  if self.cur_low  else price
            self.cur_close= price
            self.cur_vol += vol_tick
        else:
            # 분 변경 → 이전 분 확정 & 거래
            self._finalize_bar_and_trade()
            self._start_new_bar(now_min, price, vol_tick)

    def _start_new_bar(self, minute_ts, price, vol_tick):
        self.cur_bar_minute = minute_ts
        self.cur_open  = price
        self.cur_high  = price
        self.cur_low   = price
        self.cur_close = price
        self.cur_vol   = vol_tick

    # ---------------- 바 확정 → 예측 → 리밸런싱 ----------------
    def _finalize_bar_and_trade(self):
        bar = {
            "time": self.cur_bar_minute,
            "open": self.cur_open,
            "high": self.cur_high,
            "low" : self.cur_low,
            "close": self.cur_close,
            "volume": self.cur_vol
        }
        self.bars.append(bar)

        # 원시 윈도우 업데이트
        self.raw_buf.append([bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]])

        target_w = None
        if len(self.raw_buf) == WINDOW:
            target_w = self._predict_with_retry()
            self.last_target_w = target_w
            self._rebalance(target_w, ref_price=bar["close"])

        # 로그
        self._log_bar(bar, target_w)

    # ---------------- 예측 호출 (재시도/폴백) ----------------
    def _predict_with_retry(self):
        if len(self.raw_buf) < WINDOW:
            return self.last_target_w

        payload = {"ohlcv_window": [list(map(float, x)) for x in self.raw_buf]}
        last_exc = None
        for t in (0.8, 1.2):  # 두 번 시도
            try:
                r = requests.post(PREDICT_URL, json=payload, timeout=t)
                r.raise_for_status()
                data = r.json()
                tw = float(data.get("target_w", 0.0))
                if not (0.0 <= tw <= 1.0):
                    tw = max(0.0, min(1.0, tw))
                return tw
            except Exception as e:
                last_exc = e
        print("predict error:", last_exc)
        return self.last_target_w

    # ---------------- 리밸런싱/주문 (블록 단위 + 가속, 쿨다운은 그대로) ----------------
    def _rebalance(self, target_w, ref_price=None):
        # 장마감 근접시 강제청산
        if self._near_market_close():
            if self.position > 0:
                self._market_sell(self.position, ref_price or self.last_price)
            return

        now = time.time()
        if now - self.last_order_time < ORDER_COOLDOWN_SEC:  # ⬅ 주문 빈도는 기존과 동일
            return

        price = ref_price or self.last_price
        if price <= 0:
            return

        equity = self.cash + self.position * price
        if equity <= 0:
            return

        # 현재 비중
        curr_w = (self.position * price) / equity

        # 1) 작은 차이는 스킵 → 자잘한 리밸런싱 방지
        drift = target_w - curr_w
        if abs(drift) < MIN_REBALANCE_RATIO:
            return

        # 2) 가속(공격성) 적용: 목표를 더 멀리 당김
        target_w_adj = curr_w + AGGRESSION_GAIN * drift
        target_w_adj = max(0.0, min(1.0, target_w_adj))

        # 목표 수량 산출
        target_qty = int((target_w_adj * equity) // price)

        # 최대 보유가치 제한
        max_qty_by_value = int(MAX_POSITION_VALUE // price)
        target_qty = min(target_qty, max_qty_by_value)

        # 주문 delta
        delta = target_qty - self.position
        if delta == 0:
            return

        # 3) 블록 단위 주문: 너무 작은 수량은 보류하여 한 번에 크게
        block_qty_by_value = int(MIN_ORDER_VALUE_KRW // price) if MIN_ORDER_VALUE_KRW > 0 else 0
        block = max(MIN_TRADE_QTY, TRADE_BLOCK_QTY, block_qty_by_value)

        if abs(delta) < block:
            return  # 블록이 모일 때까지 대기 → 주문이 커짐

        # 가능한 블록만큼만 주문(오버슈트 방지)
        order_blocks = max(1, abs(delta) // block)
        qty = order_blocks * block
        qty = min(qty, abs(delta))
        qty = int(qty)

        if delta > 0:
            self._market_buy(qty, price)
            self.last_order_time = now
        else:
            self._market_sell(qty, price)
            self.last_order_time = now

    def _market_buy(self, qty, price):
        if qty <= 0:
            return
        if not self._can_trade():
            print("🚫 실계좌 거래 차단(ALLOW_LIVE_TRADING=False)")
            return

        # dynamicCall: 리스트 인자 방식(오버로드 안전)
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["BUY", ORDER_SCREEN, self.account, int(1), STOCK_CODE, int(qty), int(0), HOGA_MARKET, ""]
        )

        # 체결 가정(시장가): 현금/평단/포지션 업데이트
        fill_price = price * (1 + FEE_RATE + SLIPPAGE)
        cost = qty * fill_price
        if cost > self.cash:
            print("⚠️ 현금 부족. 주문 반영 생략")
            return
        if self.position > 0:
            self.avg_buy_price = (self.avg_buy_price * self.position + price * qty) / (self.position + qty)
        else:
            self.avg_buy_price = price
        self.position += qty
        self.cash -= cost
        print(f"🟢 BUY {qty} @~{int(price)} ret={ret} | pos={self.position} cash={int(self.cash)}")

    def _market_sell(self, qty, price):
        if qty <= 0 or self.position <= 0:
            return
        if not self._can_trade():
            print("🚫 실계좌 거래 차단(ALLOW_LIVE_TRADING=False)")
            return

        qty = min(qty, self.position)

        # dynamicCall: 리스트 인자 방식(오버로드 안전)
        ret = self.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["SELL", ORDER_SCREEN, self.account, int(2), STOCK_CODE, int(qty), int(0), HOGA_MARKET, ""]
        )

        fill_price = price * (1 - FEE_RATE - SLIPPAGE)
        revenue = qty * fill_price
        self.position -= qty
        self.cash += revenue
        if self.position == 0:
            self.avg_buy_price = 0.0
        print(f"🔴 SELL {qty} @~{int(price)} ret={ret} | pos={self.position} cash={int(self.cash)}")

    def _can_trade(self):
        # 실계좌 보호장치. (API는 동일하므로 안내 출력만)
        return True

    def _near_market_close(self):
        h, m = FORCE_LIQUIDATE_TIME
        nowt = now_kst()
        return (nowt.hour > h) or (nowt.hour == h and nowt.minute >= m)

    def _on_chejan(self, gubun, item_cnt, fid_list):
        # TODO: 체잔 FID 파싱하여 실제 체결가/수량 반영 (정확도↑)
        pass

    # ---------------- 로그/타임틱 ----------------
    def _log_bar(self, bar, target_w=None):
        new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["datetime","open","high","low","close","volume",
                            "cash","position","equity","target_w"])
            equity = int(self.cash + self.position * bar["close"])
            w.writerow([bar["time"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"],
                        int(self.cash), int(self.position), equity,
                        "" if target_w is None else round(float(target_w), 4)])

    def _time_tick(self):
        # 장마감 강제청산
        if self._near_market_close() and self.position > 0:
            self._market_sell(self.position, self.last_price)

# =========================
# 앱 구동
# =========================
def main():
    app = QApplication(sys.argv)
    bot = KiwoomSACClientBot()
    bot.login()
    bot.load_seed_bars(STOCK_CODE, minute_unit=1, count=SEED_BARS)
    bot.start_realtime(STOCK_CODE)

    keep = QTimer()
    keep.start(1000)
    keep.timeout.connect(lambda: None)
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
