@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then run this again.
    start https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Installing the build tool, one time...
python -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo Could not install PyInstaller. Check your internet connection and try again.
    pause
    exit /b 1
)

echo Installing the optional tray icon packages...
python -m pip install --upgrade pystray pillow
if errorlevel 1 echo The tray icon packages could not be installed. The app will still work, just without a tray icon.

echo Creating the icon...
python make_icon.py
if errorlevel 1 echo The icon could not be created. Building without one.

echo Building "OLED Screensaver.exe"...
if exist "oled.ico" (
    python -m PyInstaller --noconsole --onefile --clean --hidden-import pystray._win32 --icon "oled.ico" --add-data "oled.ico;." --name "OLED Screensaver" --distpath "." --workpath "%TEMP%\oled_build" "idle_black_app.py"
) else (
    python -m PyInstaller --noconsole --onefile --clean --hidden-import pystray._win32 --name "OLED Screensaver" --distpath "." --workpath "%TEMP%\oled_build" "idle_black_app.py"
)
if errorlevel 1 (
    echo Build failed. See the messages above.
    pause
    exit /b 1
)

rmdir /s /q "%TEMP%\oled_build" 2>nul
del "OLED Screensaver.spec" 2>nul
echo.
echo Done! "OLED Screensaver.exe" is in this folder.
echo Double-click Install.bat to install it with a Start menu entry and an easy uninstall, or just run the .exe.
pause
