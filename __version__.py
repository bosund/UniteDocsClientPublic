"""
Unite Docs - Version Information
"""

# ENESTE kilde til versionsnummeret. Compileren (compiler/kompilerv3.py) laeser
# denne linje via regex, og app/__init__.py + unitedocs.py importerer den — så
# tallet vedligeholdes kun ét sted. __version_info__ udledes af strengen.
__version__ = "9.4.3"
__version_info__ = tuple(int(p) for p in __version__.split(".") if p.isdigit())

# Release notes for this version
RELEASE_NOTES = """
Version 9.4.3 - Rettelse
- RETTET: Filer kunne ikke trækkes fra Stifinder ind i vinduet, når der
  ikke var åbne filer.

Version 9.4.2 - Rettelser
- RETTET: Lønsedler sat i fastbreddeskrift blev ulæselige ved eksport til
  .md/.txt/.epub, og fradrag mistede deres minus.
- RETTET: En ramme om tekst blev eksporteret som en tabel med indholdet
  gentaget.
- RETTET: Fluebenet "Læs også scannede sider" i anonymiseringen virkede ikke.
- RETTET: En dato som "062022" i et filnavn blev læst som 20. juni 2022 i
  stedet for juni 2022.
- Fillistens kolonne "Oprettet" hedder nu "Dokumentdato".

Version 9.4.1 - Rettelser i maskering
- RETTET: En maskering kunne slette tekst på linjen over og under det
  maskerede, når linjerne stod tæt. Det gjaldt både automatisk anonymisering,
  søgning ("Masker alle forekomster") og markering i fremviseren.
- RETTET: Anonymiseringen kunne maskere for meget, når et dokument har to
  spalter på samme linje (fx navn til venstre og feltnavn til højre på en
  lønseddel).
- RETTET: Et postnummer og en by blev overset, hvis der stod et ord med stort
  begyndelsesbogstav lige efter byen.

Version 9.4.0
- Opdateringstjekket sender et anonymt installations-id, så vi kan se, hvor
  mange der bruger programmet.

Version 9.3.1 - Sikkerhedsopdatering
- Sikkerhedsopdateringer af de indbyggede komponenter til billeder og
  dokumenter (Pillow, lxml).
- Opdateret Qt.
- RETTET: Fortryd-historikken (Ctrl+H) stod på dansk uanset sprogvalg.
"""
