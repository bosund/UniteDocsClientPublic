# Unite Docs - PDF Management Application

## 🌍 Internationalization (Important for Developers)

**This project uses dynamic internationalization with 8 supported languages.**

### For AI Assistants and Developers:
- **ALWAYS** wrap user-visible text in `_("text")` function
- **NEVER** use f-strings inside `_()`: `_(f"text {var}")` ❌
- **USE** format strings: `_("text %s") % var` ✅

### Quick Setup for New Translatable Strings:
```bash
# 1. Add _("new text") to your code
# 2. Extract new strings
python update_locales.py

# 3. Add translations in app/locales/*/LC_MESSAGES/messages.po files
# 4. Compile translations
python compile_locales.py
```

### Supported Languages:
- Danish (da)
- English (en) 
- German (de)
- French (fr)
- Spanish (es)
- Dutch (nl)
- Swedish (sv)
- Norwegian (nb_NO)

### Key Files:
- `docs/AI_INTERNATIONALIZATION_GUIDE.md` - Complete guide for AI assistants
- `docs/translation_workflow.md` - Human-readable workflow
- `update_translations.bat` - Automated workflow
- `app/localization.py` - Language management

## Development

### Prerequisites:
- Python 3.11+
- Required packages: `pip install -r requirements.txt`
- Babel for translations: `pip install babel`

### Running:
```bash
cd client
python unitedocs.py
```
# Unite Docs - PDF Management Application

## 🌍 Internationalization (Important for Developers)

**This project uses dynamic internationalization with 8 supported languages.**

### For AI Assistants and Developers:
- **ALWAYS** wrap user-visible text in `_("text")` function
- **NEVER** use f-strings inside `_()`: `_(f"text {var}")` ❌
- **USE** format strings: `_("text %s") % var` ✅

### Quick Setup for New Translatable Strings:
```bash
# 1. Add _("new text") to your code
# 2. Extract new strings
python update_locales.py

# 3. Add translations in app/locales/*/LC_MESSAGES/messages.po files
# 4. Compile translations
python compile_locales.py
```

### Supported Languages:
- Danish (da)
- English (en) 
- German (de)
- French (fr)
- Spanish (es)
- Dutch (nl)
- Swedish (sv)
- Norwegian (nb_NO)

### Key Files:
- `docs/AI_INTERNATIONALIZATION_GUIDE.md` - Complete guide for AI assistants
- `docs/translation_workflow.md` - Human-readable workflow
- `update_translations.bat` - Automated workflow
- `app/localization.py` - Language management

## Development

### Prerequisites:
- Python 3.11+
- Required packages: `pip install -r requirements.txt`
- Babel for translations: `pip install babel`

### Running:
```bash
cd client
python unitedocs.py
```

### Translation Management:
See `docs/AI_INTERNATIONALIZATION_GUIDE.md` for complete internationalization workflow.

---

## 🚀 Explore Our Other Tools / Udforsk vores andre værktøjer

If you like this project, you might also find our other applications useful! 
*Hvis du kan lide dette projekt, vil du måske også finde vores andre applikationer nyttige!*

### 🌐 [UniteApps](https://www.uniteapps.dk)
**🇩🇰 Dansk:** Vores samling af gratis beregnere og HR-værktøjer, målrettet HR-professionelle og ansatte i fagforeninger. Få lynhurtigt styr på opsigelsesvarsler efter funktionærloven, 120-dages reglen, arbejdstid, procesrente og barsel.
**🇬🇧 English:** Our toolkit of free HR calculators, specifically tailored for HR professionals and labor union employees. Easily calculate notice periods, working hours, maternity leave, and more based on Danish employment law.

### 🏢 [UniteApps CVR Opslag](https://cvr.uniteapps.dk)
**🇩🇰 Dansk:** Det intelligente CVR-opslag. Find stamdata, ejerstruktur, markedsindsigt og regnskaber for over 2 millioner danske virksomheder. Perfekt til fagforeninger og KYC – og kan integreres i din AI-assistent via vores gratis MCP-server.
**🇬🇧 English:** Intelligent business registry lookup. Find master data, ownership structures, and financial reports for all Danish companies. Built for labor unions and KYC, featuring a native MCP server for AI assistants.

### 🧠 [Arbejdsrets Guru](https://guru.uniteapps.dk)
**🇩🇰 Dansk:** Din genvej til dansk arbejdsret. Søg blandt mere end 4.600 arbejdsretlige afgørelser fra Arbejdsretten, faglige voldgifter og Tvistighedsnævnet i almindeligt sprog.
**🇬🇧 English:** Your guide to Danish labor law. Search through thousands of rulings from the Danish Labor Court and industrial arbitration boards using natural language.
