@echo off
rem ============================================================
rem  AI Player agent 停止：先走控制口 8770/stop 优雅收轮，
rem  超时（约15s）未退则提示手动结束
rem ============================================================
python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8770/stop', data=b'{}', method='POST'), timeout=10); print('已发送停止指令，等待优雅退出…')"
if errorlevel 1 (
  echo 控制口不可达——agent 可能未在运行
  exit /b 1
)
timeout /t 15 /nobreak >nul
tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | find /I "pythonw.exe" >nul && (
  echo 15s 内未退出。如确认需要强杀：taskkill /F /IM pythonw.exe
) || (
  echo agent 已退出
)
