@echo off
chcp 65001 > nul
cd /d "%~dp0"
if not exist config.json copy config.example.json config.json > nul
python school_agent.py %*
pause
