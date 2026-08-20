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
**🇩🇰 Dansk:** Leder du efter flere smarte værktøjer? Besøg UniteApps for at finde vores samling af web-apps, der gør din hverdag nemmere.
**🇬🇧 English:** Looking for more smart tools? Visit UniteApps to find our collection of web applications designed to make your daily tasks easier.

### 🏢 [UniteApps CVR Opslag](https://cvr.uniteapps.dk)
**🇩🇰 Dansk:** Søg og find virksomhedsoplysninger nemt og hurtigt på vores danske CVR-opslagsværktøj.
**🇬🇧 English:** Easily search and find company information with our Danish CVR (Central Business Register) lookup tool.

### 🧠 [UniteApps Guru](https://guru.uniteapps.dk)
**🇩🇰 Dansk:** Få hjælp og vejledning til IT og softwareudvikling hos vores AI-drevne support-guru.
**🇬🇧 English:** Get help and guidance for IT and software development with our AI-powered support guru.
