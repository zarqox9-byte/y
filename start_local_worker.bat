@echo off
title YouTube Studio Pro - Local Worker Engine
color 0b
echo ======================================================================
echo           YOUTUBE STUDIO PRO - LOCAL WORKER ENGINE (DAEMON)
echo ======================================================================
echo Starting local video processing engine...
echo Uses local unblocked ISP network for fast yt-dlp cuts and FFmpeg 9:16.
echo.
cd /d "%~dp0"
python local_worker.py --watch
pause
