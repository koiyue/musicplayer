@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ------------------------------------------------------------------
rem MusicHub 启动脚本
rem 1) 清掉代理环境变量：部分终端/工具会注入 HTTP_PROXY 与 http_proxy
rem    这类大小写重复项，导致 WebView2 与进程环境构建异常。
rem 2) 用 pythonw 启动，不留控制台窗口；诊断日志写到 run.log。
rem ------------------------------------------------------------------
for %%K in (HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy) do set "%%K="

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PYW=C:\Users\z1762\miniconda3\envs\gops-rocm\pythonw.exe"
if not exist "%PYW%" set "PYW=C:\Users\z1762\miniconda3\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"

if exist "run.log" move /y "run.log" "run.prev.log" >nul 2>&1

echo 正在启动 MusicHub ...
start "" /b "%PYW%" -u "%~dp0main.py" > "%~dp0run.log" 2>&1

echo 已启动。若窗口异常，请查看 run.log。
timeout /t 2 >nul
endlocal
