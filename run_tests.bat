@echo off
REM تشغيل اختبارات الوحدة على Windows
py -m pip install -r requirements-dev.txt
py -m pytest -q
pause
