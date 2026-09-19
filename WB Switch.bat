@echo off
rem ============================================================
rem  WB Switch - Windows launcher (v2)
rem  Kill old panel on port 5276, start fresh with latest code,
rem  wait until it answers, then open the browser.
rem ============================================================

rem 1) always kill any existing panel on 5276 (only python ones, checked in-script too)
powershell -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort 5276 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){$p=Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if($p -and $p.ProcessName -like 'python*'){Stop-Process -Id $p.Id -Force}}"

rem 2) start fresh instance with current wb_switch.py (use pythonw to hide console)
rem    Falls back to system python if the managed runtime is not present.
set "PY=%~dp0pythonw.exe"
if not exist "%PY%" set "PY=%~dp0python.exe"
if not exist "%PY%" (
  for %%P in (pythonw.exe python.exe) do (
    where %%P >nul 2>nul && set "PY=%%P" && goto found
  )
)
:found
start "" /min "%PY%" "%~dp0wb_switch.py"

rem 3) wait until it answers
:wait
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://127.0.0.1:5276/' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if not %errorlevel%==0 (timeout /t 1 /nobreak >nul & goto wait)

rem 4) open browser
start "" http://127.0.0.1:5276
exit
