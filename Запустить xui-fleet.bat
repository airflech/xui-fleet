@echo off
rem Windows: двойной клик запускает xui-fleet. Если Python не установлен, предложит установить его.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-windows.ps1"
