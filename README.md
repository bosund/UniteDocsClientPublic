# 📄 Unite Docs - The Ultimate PDF Merger & Manager

**Unite Docs** is a powerful, locally-run desktop application for Windows that makes managing, merging, and splitting PDFs and images incredibly easy.

Whether you're combining hundreds of documents, splitting large files, or dealing with password-protected PDFs, Unite Docs handles it seamlessly.

---

## 🌟 Why Choose Unite Docs?

Most PDF tools struggle when you try to merge multiple password-protected files, or when you mix PDFs with images. Unite Docs is built specifically to solve these frustrations.

### Core Features:
- 📑 **Advanced Merging & Splitting:** Effortlessly merge many PDFs and image files together, or split large PDFs into smaller parts.
- 🔐 **Unique Password Handling:** Merging multiple files with *different* passwords? No problem. Unite Docs can handle multiple passwords in the same merge—a feature you won't find in standard tools!
- 🤖 **Smart Password Guesser:** Forgot a short numerical password? The built-in password guesser can automatically crack and decrypt short numerical passwords for you.
- ✂️ **Edit & Organize:** Rotate, crop, and preview your PDF pages directly in the app.
- 🖥️ **Windows Shell Integration:** Right-click files in Windows Explorer to instantly open them in Unite Docs.
- 🌍 **Multi-language Support:** Fully translated into 8 languages (Danish, English, German, French, Spanish, Dutch, Swedish, Norwegian).

---

## 📥 Getting Started

Unite Docs is built with Python and a Tkinter GUI. To run it locally on your machine:

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
