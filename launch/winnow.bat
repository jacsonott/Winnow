@echo off
REM Double-click launcher for Windows. Starts Winnow from the install root
REM (the folder above this one). Opens a small console window; use
REM winnow.vbs instead if you'd rather it not.
cd /d "%~dp0.."
where python >nul 2>nul && ( python server.py %* & goto :eof )
where py >nul 2>nul && ( py server.py %* & goto :eof )
echo Python 3 was not found. Install Python 3, then run: python server.py
pause
