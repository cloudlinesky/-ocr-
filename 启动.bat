@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "%~dp0runtime\pythonw.exe" "%~dp0cut_by_osd_gui.py"