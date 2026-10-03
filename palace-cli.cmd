@echo off
setlocal
set "PYTHONPATH=%~dp0python;%PYTHONPATH%"
python "%~dp0python\palace_cli.py" %*
exit /b %errorlevel%
