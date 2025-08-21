수정할 부분 
1. 증거금으로도 매수가 되는 시스템
2. ai 학습모델 변경해보기(다양하게 필요)
3. 매수 매도 수량 설정(한번에 많이 or 소량 or 중간) / rebalance 함수에서 변경

실행 조건
1. 64bit 파이썬으로 server.py를 powersell에서 실행(server.py에 scaler.pkl, sac_model.zip 존재해야 함)
 #Stable-Baselines3이 32비트에서 설치가 안되는 것 같음(학습은 Colab/서버(64bit), 실시간 매매는 Kiwoom PC(32bit)으로 분리)
3. 32bit 파이썬으로 main2.py를 실행 -> 자동 매매 시작
 
