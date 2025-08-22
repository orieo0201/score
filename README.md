수정할 부분 
1. ai 학습모델 변경해보기(다양하게 필요)
2. 매수 매도 수량 변경해보기(주문 수량 수 or 주문 빈도) / rebalance 함수에서 변경

수정된 부분 
1. 주문빈도 5초 -> 1초로 변경
2. 증거금으로도 매수가 되는 시스템 막음
3. 15시19분 강제 매도가 일부에서 전체로 바꿈

실행 조건
1. 64bit 파이썬으로 server.py를 powersell에서 실행(server.py에 scaler.pkl, sac_model.zip 존재해야 함)
 #Stable-Baselines3이 32비트에서 설치가 안되는 것 같음(학습은 Colab/서버(64bit), 실시간 매매는 Kiwoom PC(32bit)으로 분리)

cmd 접속 후 
C:\ai64\Scripts\activate.bat
cd C:\ai64\app
python server.py

3. 32bit 파이썬으로 main2.py를 실행 -> 자동 매매 시작
 
*pkl, zip파일은 자주 변경될 예정
