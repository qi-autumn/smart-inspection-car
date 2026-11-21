@echo off
title V3 car_002 simulator
cd /d "%~dp0"
python device\car_mqtt_client.py --device car_002
pause
