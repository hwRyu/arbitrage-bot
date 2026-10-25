#!/usr/bin/env bash
# 봇 본체 자원 감시 (윈도우 · Git Bash): 1분마다 logs/resource_watch.csv 에 한 줄 적고,
# 기준선보다 튀면 stdout 으로 알린다 → Claude 의 Monitor 도구나 터미널에서 그대로 본다.
#   알림 기준: 메모리(WorkingSet) +50 MB, 스레드 +5, 핸들 +200 이상 / 30분마다 요약 한 줄
#   알려진 정상 패턴: 기동 후 약 70분 동안 366 → 450~470 MB 로 오른 뒤 평탄 (파이썬 객체 수는 안 늘어남, 누수 아님)
#   실행:  bash tools/monitor/resource_watch.sh      (멈추려면 Ctrl+C — 봇과는 무관한 별도 프로세스)
HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p logs
CSV="logs/resource_watch.csv"
PS1="$(cygpath -w "$HERE/resource_sample.ps1" 2>/dev/null || echo "$HERE/resource_sample.ps1")"
[ -f "$CSV" ] || echo "time,pid,ws_mb,private_mb,threads,handles,cpu_s,live_db_mb,bot_log_mb" > "$CSV"
base_pid=""; base_ws=""; base_th=""; base_h=""; n=0
while true; do
  line=$(powershell -NoProfile -ExecutionPolicy Bypass -File "$PS1" 2>/dev/null | tr -d '\r')
  ts=$(date '+%F %T')
  if [ "$line" = "nopid" ] || [ -z "$line" ]; then echo "$ts 봇 프로세스를 못 찾음 (재기동 중?)"; sleep 60; continue; fi
  db=$(stat -c %s data/live.db 2>/dev/null || echo 0); lg=$(stat -c %s logs/bot.log 2>/dev/null || echo 0)
  db_mb=$(awk "BEGIN{printf \"%.1f\", $db/1048576}"); lg_mb=$(awk "BEGIN{printf \"%.1f\", $lg/1048576}")
  echo "$ts,$line,$db_mb,$lg_mb" >> "$CSV"
  IFS=, read -r pid ws priv th h cpu <<< "$line"
  if [ "$pid" != "$base_pid" ]; then      # 봇이 재기동되면 PID 가 바뀐다 → 기준선 다시
    base_pid=$pid; base_ws=$ws; base_th=$th; base_h=$h; n=0
    echo "$ts 기준선 PID $pid: 메모리 ${ws} MB (커밋 ${priv} MB) · 스레드 $th · 핸들 $h"
  fi
  dws=$(awk "BEGIN{printf \"%.1f\", $ws-$base_ws}"); dth=$((th-base_th)); dh=$((h-base_h))
  if awk "BEGIN{exit !($dws>=50)}" || [ $dth -ge 5 ] || [ $dh -ge 200 ]; then
    echo "$ts [자원 증가] 메모리 ${ws} MB (+${dws}) · 커밋 ${priv} MB · 스레드 $th (+$dth) · 핸들 $h (+$dh) · DB ${db_mb} MB · 로그 ${lg_mb} MB"
    base_ws=$ws; base_th=$th; base_h=$h   # 다시 알리려면 그 위에서 또 그만큼 늘어야 한다
  fi
  n=$((n+1))
  if [ $((n % 30)) -eq 0 ]; then echo "$ts [자원 요약 30분] 메모리 ${ws} MB · 커밋 ${priv} MB · 스레드 $th · 핸들 $h · CPU 누적 ${cpu}s · DB ${db_mb} MB · 로그 ${lg_mb} MB"; fi
  sleep 60
done
