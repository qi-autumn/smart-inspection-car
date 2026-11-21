@echo off
rem Start the V3 fleet backend on port 8000 (run from this folder).
title V3 fleet backend
cd /d %~dp0
python server\app.py
pause
