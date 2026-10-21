# 24시간 돌리기 (서버 배포)

> 실행 환경이 정해지면 그에 맞춰 보완한다. 지금은 '고정 IP 리눅스 서버 + systemd' 기준.

## 왜 서버가 필요한가
- 업비트 API 키는 **등록한 IP 에서만** 동작한다. 집 인터넷은 IP 가 바뀔 수 있다.
- 맥이 잠자기·재부팅하면 봇도 멈춘다. 포지션을 든 채 멈추면 청산을 못 한다.
- 예: AWS Lightsail / EC2 (고정 IP = Elastic IP), 오라클 클라우드 무료 VM, 국내 클라우드. 서울 리전이면 업비트·바이비트 둘 다 지연이 작다.

## 설치 (우분투 기준)
```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
git clone <이 저장소> ~/arbitrage-bot && cd ~/arbitrage-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # 키가 들어가니 나만 읽게
nano .env                                  # docs/live.md 5번 순서대로
python3 -m kimp.live_check                 # ❌ 없을 때까지
```
- 서버의 공인 IP 를 업비트·바이비트 키 IP 허용 목록에 넣는다: `curl -s ifconfig.me`

## 자동 재시작 (systemd)
`/etc/systemd/system/kimp-bot.service`
```ini
[Unit]
Description=kimp hedge bot
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/arbitrage-bot
ExecStart=/home/ubuntu/arbitrage-bot/.venv/bin/python main.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kimp-bot     # 켜기 + 부팅 때 자동
sudo systemctl status kimp-bot
journalctl -u kimp-bot -f                # 화면 로그 (파일 로그는 logs/)
sudo systemctl stop kimp-bot             # 끄기 (Ctrl+C 와 같음: 하던 스캔 마치고 종료)
```
- `KillSignal=SIGINT`: 끌 때 Ctrl+C 처럼 안전하게 끝낸다. 열린 포지션은 DB 에 남고, 다시 켜면 이어서 관리한다.
- 꺼졌다 켜질 때 진입·청산 중이던 포지션은 자동으로 이어서 정리한다 (docs/live.md 2번).

## 백업
- `data/live.db` 가 실거래 기록 전부다. 하루 한 번 복사해 두기: `cp data/live.db backups/live-$(date +%F).db`
