@echo off
rem Double-click to start ProjectWatch. First run installs it (needs Python 3.11+).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-projectwatch.ps1" %*
if errorlevel 1 pause
