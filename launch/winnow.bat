@echo off
REM Double-click launcher for Windows. Starts Winnow from the install root
REM (the folder above this one). Opens a small console window; use
REM winnow.vbs instead if you'd rather it not.
cd /d "%~dp0.."
REM Each candidate is RUN before it is chosen, not looked up with where.
REM On a machine without the Store Python, where python still finds the
REM Microsoft Store alias stub in WindowsApps, which only prints that
REM Python was not found and exits 9009 - and this script used to stop
REM there without ever trying the py launcher.
python -c "import sys; sys.exit(sys.version_info[0] != 3)" >nul 2>nul && ( python server.py %* & goto :eof )
py -3 -c "import sys" >nul 2>nul && ( py -3 server.py %* & goto :eof )
echo Python 3 was not found. Install Python 3, then run: python server.py
pause
