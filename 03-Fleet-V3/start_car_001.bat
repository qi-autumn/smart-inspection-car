@echo off
title V3 car_001 simulator
cd /d "%~dp0"
python device\car_mqtt_client.py --device car_001
pause
