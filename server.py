# server.py — SAC 추론 서버 (64bit 전용)
# 실행:  (venv 활성화 후)  python server.py
# 요청:  POST /predict  { "ohlcv_window": [[o,h,l,c,v], ... 12개] }

import os, time, pickle
import numpy as np
from flask import Flask, request, jsonify
from stable_baselines3 import SAC

# ==== 학습 때 쓰던 설정과 일치해야 합니다 ====
WINDOW = 12          # Colab 학습/테스트 때의 window_size
N_FEAT = 5           # OHLCV 5개
MODEL_PATH  = os.environ.get("MODEL_PATH",  "sac_model.zip")
SCALER_PATH = os.environ.get("SCALER_PATH", "scaler.pkl")

app = Flask(__name__)
model = None
scaler = None

def load_artifacts():
    global model, scaler
    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"MODEL not found: {MODEL_PATH}")
    if not os.path.exists(SCALER_PATH):
        raise FileNotFoundError(f"SCALER not found: {SCALER_PATH}")
    model = SAC.load(MODEL_PATH)
    with open(SCALER_PATH, "rb") as f:
        scaler = pickle.load(f)

@app.get("/health")
def health():
    ok = (model is not None and scaler is not None)
    return jsonify({"ok": ok, "window": WINDOW})

@app.post("/predict")
def predict():
    """
    payload: {
      "ohlcv_window": [[open,high,low,close,volume], ...]  # 길이=WINDOW
    }
    returns: { "target_w": float in [0,1], "ts": epoch }
    """
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"error": f"invalid JSON: {e}"}), 400

    if not data or "ohlcv_window" not in data:
        return jsonify({"error": "missing field: ohlcv_window"}), 400

    # (WINDOW, 5) 체크
    try:
        window = np.array(data["ohlcv_window"], dtype=float)
    except Exception:
        return jsonify({"error": "ohlcv_window must be a 2D numeric list"}), 400

    if window.ndim != 2 or window.shape[0] != WINDOW or window.shape[1] != N_FEAT:
        return jsonify({"error": f"expected shape ({WINDOW},{N_FEAT}), got {tuple(window.shape)}"}), 400

    # Colab과 동일한 스케일러로 변환
    window_scaled = scaler.transform(window)            # (W,5)
    obs = np.hstack([window_scaled.flatten(), window_scaled[-1][0]]).astype(np.float32)
    action, _ = model.predict(obs, deterministic=True)

    # [-1,1] → [0,1] 변환 + 클램프
    target_w = float((action[0] + 1.0) / 2.0)
    target_w = max(0.0, min(1.0, target_w))
    return jsonify({"target_w": target_w, "ts": int(time.time())})

if __name__ == "__main__":
    load_artifacts()
    # 로컬 PC에서만 쓸거면 127.0.0.1로 충분
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=False)
