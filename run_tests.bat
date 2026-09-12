@echo off
REM تشغيل كل اختبارات المشروع على Windows
py -m pip install -r requirements-dev.txt
py -m pytest -q
pause
