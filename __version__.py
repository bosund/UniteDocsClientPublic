"""
Unite Docs - Version Information
"""

# ENESTE kilde til versionsnummeret. Compileren (compiler/kompilerv3.py) laeser
# denne linje via regex, og app/__init__.py + unitedocs.py importerer den — så
# tallet vedligeholdes kun ét sted. __version_info__ udledes af strengen.
__version__ = "9.2.0"
__version_info__ = tuple(int(p) for p in __version__.split(".") if p.isdigit())

# Release notes for this version
RELEASE_NOTES = """
Version 9.2.0 - Kopier tekst med pseudonymer
- Ny knap "Kopiér tekst": dokumentets tekst lægges i udklipsholderen, og
  personoplysninger kan byttes ud med pseudonymer (Person 1, Advokat 1, ...).
  Samme person faar samme pseudonym i alle filer. Navneoversigten kan gemmes.
- Ny knap "Tekstgenkendelse": scannede sider kan derefter markeres,
  fremhaeves og maskeres som alle andre.
- "Vaelg sider": flere sider kan markeres uden tastatur, og Flet og gem,
  Gem enkeltfiler, Eksportér og Kopiér tekst foelger markeringen.
- Velkomstskaerm med overblik over programmets funktioner.
- Farveknappen viser nu selve farven, og Marker og Fremhaev er samlet i én
  gruppe. Vinduet aabner stort nok til at alle knaptekster staar helt.
- RETTET: tabeller i .md/.txt/.epub-eksport, tekstmarkering foelger linjerne,
  og vaerktoejernes tooltips er nu oversat.
"""
