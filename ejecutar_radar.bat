@echo off
REM Lanzador para el Programador de tareas de Windows.
REM Ajusta la ruta de Python si no está en el PATH.
cd /d "%~dp0"
if not exist data mkdir data
python radar_normativo.py >> data\radar.log 2>&1
