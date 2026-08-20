@echo off
cd /d "%~dp0"
echo ==========================================
echo    Unite Docs - Oversættelsesværktøj
echo ==========================================
echo.

echo [1/3] Udtrækker nye tekster fra koden...
python update_locales.py
if errorlevel 1 (
    echo FEJL: Kunne ikke opdatere oversættelsesfiler
    pause
    exit /b 1
)

echo.
echo [2/3] Nye tekster er udtrukket!
echo Åbn nu .po filerne i app/locales/*/LC_MESSAGES/ og tilføj oversættelser
echo for de tomme msgstr felter.
echo.
echo Tryk en tast når du er færdig med at tilføje oversættelser...
pause

echo.
echo [3/3] Kompilerer oversættelsesfiler...
python compile_locales.py
if errorlevel 1 (
    echo FEJL: Kunne ikke kompilere oversættelsesfiler
    pause
    exit /b 1
)

echo.
echo ==========================================
echo     Oversættelser opdateret succesfuldt!
echo ==========================================
echo.
echo Du kan nu teste ændringerne ved at køre:
echo python unitedocs.py
echo.
pause
