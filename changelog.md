# Ændringslog

## Version 7.5.1 (2026-02-20)
*   **Ydeevne:**
    *   Yderligere Python-optimeringer og finpudsning af C-accelerator.
*   **Udvikling:**
    *   Tilføjet omfattende benchmark-scripts til performance-måling.
    *   Nye instruktioner til AI-agenter (`agents.md` og workflow-guider).
    *   Opdateret test-suite for bedre dækning af PDF-manipulation.

## Version 7.5.0 (2025-12-16)
*   **Ydeevne:**
    *   Implementeret C-accelerator til password gætte funktion (10-100x hurtigere).
    *   Native C-kode eliminerer Python interpreter overhead.
    *   Automatisk fallback til Python hvis C extension ikke tilgængelig.
*   **Tekniske forbedringer:**
    *   Tilføjet `guesser_c.cp311-win_amd64.pyd` C extension modul.
    *   Implementeret `bruteforce_numeric()` og `check_candidate_list()` i C.
    *   Dynamisk PDFium DLL loading for runtime fleksibilitet.
    *   Opdateret Nuitka build konfiguration til at inkludere C extension.

## Version 7.4.5 (2025-12-16)
*   **Kritiske fejlrettelser (fra Code Review):**
    *   [LØST] Global Variabel Injektion: Refaktoreret til at bruge `LocalizationManager` Singleton, hvilket fjerner usikker injektion af `_`.
    *   [LØST] Bred Exception Handling: Opdateret til at fange specifikke exceptions (`pikepdf.PdfError`, `OSError` mv.) i `pdf_utils.py` og `main_app.py`.
    *   [LØST] Shell Injection Risiko: `subprocess.Popen` i `kompilerv3.py` bruger nu `shell=False` og liste-argumenter.
    *   [LØST] Race Condition i Preview: Tilføjet sikkerhedstjek i `preview_window.py` for at undgå crash hvis filen slettes mens preview er åbent.

## Version 7.4.4 (2025-08-28)
*   **Fejlrettelser:**
    *   Rettet drag-and-drop animation så den ikke længere giver _tkinter.TclError: bad geometry specifier.
    *   Fjernet mellemrum i geometry-string i _on_drag_motion.

## Version 7.4.3 (2025-01-27)
*   **Kritiske fejlrettelser:**
    *   Kompileret sprogfiler


## Version 7.4.2 (2025-01-27)

*   **Kritiske fejlrettelser:**
    *   Rettede hukommelseslæk i thumbnail-generering ved at implementere centraliseret thread management.
    *   Løste race conditions i tree item updates ved at tilføje proper synchronization og validation.
    *   Erstattet individuelle threads med en central thumbnail worker thread og queue system.
    *   Tilføjet periodisk cleanup af thumbnail cache og threads hver 30. sekund.
    *   Forbedret application shutdown med proper thread cleanup og resource management.
*   **Tekniske forbedringer:**
    *   Implementeret `_thumbnail_worker()` metode til at håndtere thumbnail requests fra en queue.
    *   Tilføjet `_should_process_thumbnail()` metode til validation af tree item existence.
    *   Forbedret `_update_tree_item_thumbnail()` med comprehensive error handling og race condition protection.
    *   Tilføjet `_periodic_cleanup()` metode til automatisk cleanup af orphaned resources.
    *   Enhanced `_on_closing()` metode med proper thread shutdown og queue clearing.

## Version 7.4.1 (2025-08-11)

*   **Fejlrettelser:**
    *   Rettede kritisk fejl hvor PDF-filer ikke viste thumbnails korrekt.
    *   Løste PIL.UnidentifiedImageError ved thumbnail-generering for PDF-filer.
    *   Thumbnail-generering bruger nu pdf_renderer i stedet for PIL.Image.open direkte.
    *   PDF-filer viser nu igen korrekte preview-thumbnails i stedet for tre prikker.

## Version 7.4.0 (2025-08-11)

*   **PDF-bibliotek migration:**
    *   Skiftet fra PyMuPDF til pikepdf for bedre ydeevne og stabilitet.
    *   Kasseret pymupdf afhængigheden og implementeret ny PDF-processor.
    *   Forbedret håndtering af PDF-rendering og manipulation.
*   **Krypteringsindikatorer:**
    *   Tilføjet hængelås-ikon til krypterede filer i fillisten.
    *   Ny lock.png ikon tilføjet til /icons mappen.
    *   Bedre visuel indikation af filkrypteringsstatus.
*   **Beskæringsfunktionalitet (Crop):**
    *   Rettede fejl hvor thumbnails ikke viste beskårede områder korrekt.
    *   Preview vinduet viser nu originalt ubeskåret billede med rød crop-box overlay.
    *   Implementeret _get_original_pil_image metode for bedre crop preview.
    *   Forbedret crop state management med save/cancel funktionalitet.
    *   Tilføjet debug logging for crop bounds validering.
*   **Tekniske forbedringer:**
    *   Nye test-filer til validering af PDF-processing funktionalitet.
    *   Opdateret requirements.txt med nye afhængigheder.
    *   Forbedret fejlhåndtering og bounds checking for billedmanipulation.
    *   Refaktoreret PDF-rendering arkitektur for bedre modularity.

## Version 7.3.0 (2025-08-08)

*   **Internationalisering:**
    *   Implementeret fuldt sprogskifte uden genstart af applikationen.
    *   Tilføjet 8 understøttede sprog: Dansk, Engelsk, Tysk, Fransk, Spansk, Hollandsk, Svensk og Norsk.
    *   Preview vinduet er nu fuldt internationaliseret med oversatte knapper og beskeder.
    *   Alle UI-elementer opdateres øjeblikkeligt når sprog skiftes.
*   **Indstillingsvindue:**
    *   Indstillingsvinduet åbner nu centreret mod højre øverste hjørne af hovedvinduet.
    *   OK-dialoger vises nu med custom positionering (lidt til venstre og nedad for indstillingsvinduet).
    *   Forbedret brugeroplevelse med konsistent dialog positionering.
*   **Konfigurationsfiler:**
    *   Config.ini gemmes nu i samme mappe som password cache (AppData\Roaming\Unite Docs).
    *   Alle brugerdata er nu samlet ét sted for bedre organisation.
*   **Tekniske forbedringer:**
    *   Flyttet app data funktioner til utils.py for bedre kodeorganisation.
    *   Automatisk kompilering af oversættelsesfiler.
    *   Forbedret håndtering af gettext og babel integration.

## Version 7.2.1 (2025-08-07)

*   **Fejlrettelse:**
    *   Rettet fejl i "Gem enkeltfiler" funktionen, så den kun behandler filer med status "Dekrypteret".

## Version 7.2.0 (2025-08-07)

*   **UI:**
    *   Det er nu muligt at slette markerede filer i listen ved at trykke på `Delete`-tasten.

## Version 7.1.0 (2025-08-07)

*   **Kompileringsværktøj:**
    *   Kompileren bruger nu venv fra client-mappen og håndterer stier til icons og installer korrekt.
    *   Installerscriptet virker uanset placering.
*   **Brute-force status:**
    *   Statusindikator viser nu antal cifre, progressbar og estimeret tid (ETA) for password-test.
    *   Tekst ændret fra "Brute-forcer" til "Tester".
    *   Statusvindue bredde justeret til 500px.
*   **UI:**
    *   Mindre justeringer af statusvindue og progressbar.

## Version 7.0.0 (2025-08-07)

*   **Refactoring:**
    *   The application has been split into multiple Python files to improve maintainability and readability.
    *   The code is now organized into a new `app` sub-directory, with separate modules for the main application, preview window, PDF utilities, and password guessing.
*   **Configuration:**
    *   A new `config.ini` file has been added to allow users to configure the application theme and password bruteforce length without editing the code.
*   **Performance:**
    *   The password guesser has been optimized to use a `multiprocessing.Pool`, which should significantly improve its speed.

