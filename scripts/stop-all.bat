@echo off
REM =============================================================================
REM stop-all.bat — Windows 侧停止所有服务
REM =============================================================================
REM 终止 llama-server.exe 进程，清理端口 8090 占用。
REM 以管理员身份运行可获得完整的端口清理能力。
REM =============================================================================

setlocal enabledelayedexpansion

echo [停止] 正在停止 Windows 侧服务...

REM ---- 终止 llama-server.exe 进程 ----
tasklist /fi "imagename eq llama-server.exe" 2>nul | find /i "llama-server.exe" >nul
if %ERRORLEVEL% equ 0 (
    echo [进程] 正在终止 llama-server.exe...
    taskkill /f /im llama-server.exe >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        echo [完成] llama-server.exe 已终止。
    ) else (
        echo [警告] 无法终止 llama-server.exe，请手动关闭。
    )
) else (
    echo [信息] llama-server.exe 未在运行。
)

REM ---- 清理端口 8090 ----
echo [端口] 检查端口 8090 占用情况...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8090 "') do (
    set PID=%%a
    echo [端口] 端口 8090 被 PID !PID! 占用，正在终止...
    taskkill /f /pid !PID! >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        echo [完成] PID !PID! 已终止，端口 8090 已释放。
    ) else (
        echo [警告] 无法终止 PID !PID!，请手动处理或使用管理员权限运行此脚本。
    )
)

REM 再次确认端口已释放
netstat -ano | findstr ":8090 " >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [确认] 端口 8090 已释放。
) else (
    echo [警告] 端口 8090 可能仍有残留占用。
)

echo [完成] Windows 侧服务已全部停止。
endlocal
