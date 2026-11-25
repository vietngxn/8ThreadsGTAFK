@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconsole --onefile --collect-all sounddevice --collect-all soundfile --add-data "ringtone;ringtone" --name GTAEco gta_eco.py
echo.
echo Xong! File nam o: dist\GTAEco.exe
pause
