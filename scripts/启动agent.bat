@echo off
rem ============================================================
rem  AI Player agent 独立启动（detach 模式）
rem  - 从任意位置启动 agent（不依赖游戏）；游戏随后启动时模组
rem    会检测到实例已在跑并复用，不会重复拉起
rem  - 无窗口（pythonw）；停止请用 停止agent.bat
rem ============================================================
setlocal
rem ★安装根 = 本脚本的上两级（部署目录含 run.py + core/）
set "AIPLAYER_HOME=%~dp0..\.."
set "AIPLAYER_HOME=%AIPLAYER_HOME:~0,-1%"
cd /d "%AIPLAYER_HOME%"

start "AI-agent" /b pythonw run.py --daemon
echo agent 已后台启动（AIPLAYER_HOME=%AIPLAYER_HOME%）
endlocal
