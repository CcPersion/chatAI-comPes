@echo off
REM =============================================================================
REM start-llama.bat — Windows 侧启动 llama.cpp llama-server
REM =============================================================================
REM 用途: 启动 llama-server.exe，加载 Qwen3-8B Q4_K_M 模型，提供 OpenAI 兼容 API。
REM 依赖: llama-server.exe 与模型文件已就位（见下方路径配置）。
REM 运行: 双击或在 CMD/PowerShell 中执行此脚本。
REM =============================================================================

setlocal enabledelayedexpansion

REM ---- 路径配置（按实际安装修改） ----
set LLAMA_SERVER=D:\models\llama.cpp\llama-server.exe
set MODEL_PATH=D:\models\llama.cpp\Qwen3-8B-Instruct-Q4_K_M.gguf
REM 24GB 卡可切换为:
REM set MODEL_PATH=D:\models\llama.cpp\Qwen3-14B-Instruct-Q4_K_M.gguf

REM ---- 端口与监听配置 ----
set HOST=127.0.0.1
set PORT=8090

REM ---- 检查文件是否存在 ----
if not exist "%LLAMA_SERVER%" (
    echo [错误] 找不到 llama-server.exe，请确认路径: %LLAMA_SERVER%
    pause
    exit /b 1
)

if not exist "%MODEL_PATH%" (
    echo [错误] 找不到模型文件，请确认路径: %MODEL_PATH%
    pause
    exit /b 1
)

echo [启动] 正在启动 llama-server...
echo   模型: %MODEL_PATH%
echo   监听: %HOST%:%PORT%

REM ---- 启动 llama-server ----
REM 说明: -ngl 999 将全部层卸载到 GPU（CUDA 加速）。
REM 若 WSL2 localhostForwarding 不可用，需要改为 --host 0.0.0.0 并添加防火墙规则：
REM   1. 将下方 %HOST% 改为 0.0.0.0
REM   2. 以管理员身份运行:
REM      netsh advfirewall firewall add rule name="llama-server" dir=in action=allow protocol=tcp localport=%PORT%
REM   3. WSL2 内使用 Windows 主机 IP（/etc/resolv.conf 中的 nameserver）访问。
start "llama-server" "%LLAMA_SERVER%" ^
    --model "%MODEL_PATH%" ^
    --host %HOST% ^
    --port %PORT% ^
    -ngl 999 ^
    --ctx-size 4096 ^
    --batch-size 512 ^
    --threads 8

REM ---- 轮询等待就绪 ----
echo [等待] 正在等待 llama-server 就绪...

set MAX_RETRIES=60
set RETRY_COUNT=0

:check_health
REM 使用 PowerShell 发起 HTTP GET /health 请求
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://%HOST%:%PORT%/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1

if %ERRORLEVEL% equ 0 (
    echo [就绪] llama-server 就绪! 监听 http://%HOST%:%PORT%
    goto :done
)

set /a RETRY_COUNT+=1
if %RETRY_COUNT% geq %MAX_RETRIES% (
    echo [超时] llama-server 启动超时（已等待 %MAX_RETRIES% 秒），请检查服务是否正常启动。
    pause
    exit /b 1
)

REM 每秒重试一次
timeout /t 1 /nobreak >nul
goto :check_health

:done
echo [信息] llama-server 已就绪，可以启动 WSL2 侧服务。
endlocal
