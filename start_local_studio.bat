@echo off
title YouTube Studio Pro - Local Web Server
color 0a
echo ======================================================================
echo           YOUTUBE STUDIO PRO - LOCAL STUDIO SERVER
echo ======================================================================
echo Launching YouTube Studio Pro locally at http://localhost:5000...
echo.
cd /d "%~dp0"
start "" http://localhost:5000
python app.py
pause
