@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Creando entorno virtual...
    py -3 -m venv .venv
)

call ".venv\Scripts\activate.bat"

echo Instalando/actualizando dependencias...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo.
echo Iniciando Cotizador de Neumaticos...
echo (se abrira el navegador automaticamente; no cierres esta ventana)
python app.py

pause
