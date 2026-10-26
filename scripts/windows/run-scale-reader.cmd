@echo off
REM Wrapper for health-sync scale reader. Runs in a loop, restarts reader.py on exit.
REM Install directory: C:\Tools\health-sync-scale (set INSTALL_DIR to override)
REM Log rotation: if reader.log > 10 MB, move to reader.log.1 before each start.
REM Passes --idle-exit 600 so the reader exits on a silent BLE stall (wrapper restarts it).

setlocal

if not defined INSTALL_DIR set "INSTALL_DIR=C:\Tools\health-sync-scale"
set "PYTHON=%INSTALL_DIR%\venv\Scripts\python.exe"
set "READER=%INSTALL_DIR%\reader.py"
set "LOG=%INSTALL_DIR%\reader.log"
set "ADDRESS=88:22:B2:BF:9B:EC"
set "URL=http://127.0.0.1/scale"
set "HOST=health-sync.prod.local"
set "TOKEN_FILE=%INSTALL_DIR%\token.txt"
set "PENDING_FILE=%INSTALL_DIR%\pending.jsonl"
set "IDLE_EXIT=600"

:loop
REM Rotate log if it exceeds 10 MB (10485760 bytes)
if exist "%LOG%" (
    for %%A in ("%LOG%") do if %%~zA GTR 10485760 (
        if exist "%LOG%.1" del /q "%LOG%.1"
        move /y "%LOG%" "%LOG%.1" >nul
    )
)
echo [%date% %time%] starting reader >> "%LOG%"
"%PYTHON%" "%READER%" --address "%ADDRESS%" --url "%URL%" --host "%HOST%" --token-file "%TOKEN_FILE%" --pending-file "%PENDING_FILE%" --idle-exit %IDLE_EXIT% >> "%LOG%" 2>&1
echo [%date% %time%] reader exited with code %ERRORLEVEL%, restarting in 10 s >> "%LOG%"
REM ping, not timeout: timeout needs an interactive console and fails at once in a background task
ping -n 11 127.0.0.1 >nul
goto loop
