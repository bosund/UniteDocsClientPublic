# 🛡️ Unite Docs - Secure PDF Anonymization

**Unite Docs** is a powerful, locally-run desktop application designed to securely and automatically anonymize PDF documents. It completely removes sensitive personal information (GDPR data) from your files in minutes, saving you hours of manual work and ensuring total compliance.

---

## 🛑 The Problem with "Black Boxes"
When you draw a black box over text in standard PDF editors, the underlying text often remains. Anyone can simply highlight the box, press "Copy", and read the hidden data. To truly anonymize a document, professionals often resort to printing and scanning pages—a slow, error-prone, and frustrating process.

## 💡 The Solution: True Redaction
Unite Docs changes the game. It **finds and permanently removes** the underlying text from the document. The data isn't just covered; it's gone. 

### Key Features:
- 🕵️ **Smart Detection:** Automatically finds CPR numbers (with validation), email addresses, and phone numbers.
- 🎯 **Custom Keywords:** Enter specific names, employee numbers, or addresses, and the tool will find and remove every instance across hundreds of pages instantly.
- 🔒 **100% Local & Secure:** Your files never leave your computer. There are no servers, no cloud uploads, and no logs. Total privacy guaranteed.
- 🧹 **Metadata Cleaning:** Automatically strips author names and other hidden metadata from the final PDF.
- 🌍 **Multi-language Support:** Fully translated into 8 languages (Danish, English, German, French, Spanish, Dutch, Swedish, Norwegian).

---

## 📥 Getting Started

Unite Docs is built with Python. To run it locally on your machine:

### Prerequisites
- Python 3.11+
- Git

### Installation
```bash
# Clone the repository
git clone https://github.com/bosund/UniteDocsClientPublic.git
cd UniteDocsClientPublic

# Install dependencies
pip install -r requirements.txt

# Run the application
python unitedocs.py
```

## 🛠️ For Developers
Unite Docs is open for community contributions. If you want to add new features or translations, here is a quick guide:

- **Adding Translations:** We use `babel` for i18n. Always wrap user-visible text in `_("text")`. Use `python update_locales.py` to extract strings and `python compile_locales.py` to compile them. See `docs/AI_INTERNATIONALIZATION_GUIDE.md` for details.

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
