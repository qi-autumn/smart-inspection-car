@echo off
chcp 65001 >nul
title V3 car_003
cd /d %~dp0
python device\car_mqtt_client.py --device car_003
pause
