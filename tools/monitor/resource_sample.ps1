# 봇 본체(main.py 자식 프로세스) 자원 한 줄:  pid,ws_mb,private_mb,threads,handles,cpu_s   (못 찾으면 nopid)
#   python main.py 는 런처 + 본체 두 프로세스로 뜬다. 부모 PID 가 같은 목록 안에 있는 쪽이 본체.
#   쓰는 곳: tools/monitor/resource_watch.sh (1분마다 호출) — 직접 한 번 보려면
#     powershell -NoProfile -ExecutionPolicy Bypass -File tools/monitor/resource_sample.ps1
$ps = Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -like '*main.py*' }
if (-not $ps) { "nopid"; exit }
$ids = @($ps | ForEach-Object { $_.ProcessId })
$child = $ps | Where-Object { $ids -contains $_.ParentProcessId } | Select-Object -First 1
if (-not $child) { $child = $ps | Select-Object -First 1 }
$p = Get-Process -Id $child.ProcessId
"{0},{1},{2},{3},{4},{5}" -f $p.Id, [math]::Round($p.WorkingSet64/1MB,1), [math]::Round($p.PrivateMemorySize64/1MB,1), $p.Threads.Count, $p.HandleCount, [math]::Round($p.TotalProcessorTime.TotalSeconds,0)
