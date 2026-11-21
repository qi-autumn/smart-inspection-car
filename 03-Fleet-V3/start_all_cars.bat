@echo off
rem Start both car simulators in separate windows (independent client_id / secret)
start "V3 car_001" cmd /c "%~dp0start_car_001.bat"
start "V3 car_002" cmd /c "%~dp0start_car_002.bat"
echo Both simulators launched in separate windows.
