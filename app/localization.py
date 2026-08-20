"""
INTERNATIONALIZATION (i18n) MODULE FOR UNITE DOCS

This module handles dynamic language switching without application restart.
AI ASSISTANTS: When adding new user-visible strings, always use _("text") function.

Usage in code:
    from .localization import LocalizationManager
    
    # Initialize
    LocalizationManager.initialize("en")
    
    # Get the functionality
    _ = LocalizationManager.get_text
    
    # Or get the singleton
    loc = LocalizationManager.get_instance()
    _ = loc.get_text

After adding new _("strings"), run:
    1. python update_locales.py (extract new strings)
    2. Add translations to .po files manually  
    3. python compile_locales.py (compile to binary)
"""

import gettext
import locale
import os
import sys
from .config import AppConfig
from .logging_config import get_logger

logger = get_logger(__name__)

# Supported languages - add new languages here
LANGUAGES = {
    "en": "English",
    "da": "Dansk",
    "sv": "Svenska",
    "nb_NO": "Norsk",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "nl": "Nederlands"
}

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'locales')

# Performance: reverse lookup dict avoids O(n) scan in get_language_code()
_LANGUAGE_NAMES_REVERSE: dict[str, str] = {v: k for k, v in LANGUAGES.items()}

# Performance: module-level constant avoids recreating this dict on every set_language() call
_WINDOWS_LOCALES: dict[str, str] = {
    "en": "English_United States.1252",
    "da": "Danish_Denmark.1252",
    "sv": "Swedish_Sweden.1252",
    "nb_NO": "Norwegian (Bokmål)_Norway.1252",
    "de": "German_Germany.1252",
    "fr": "French_France.1252",
    "es": "Spanish_Spain.1252",
    "nl": "Dutch_Netherlands.1252"
}

class LocalizationManager:
    _instance = None
    _current_lang = "en"
    _translator = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def initialize(cls, lang_code=None):
        """Initializes the singleton with a specific language or from config."""
        instance = cls.get_instance()
        instance.set_language(lang_code)
        return instance

    @classmethod
    def get_text(cls, message):
        """Static wrapper for getting translated text."""
        instance = cls.get_instance()
        if instance._translator:
            return instance._translator.gettext(message)
        return gettext.gettext(message)

    def __init__(self):
        if LocalizationManager._instance is not None:
             raise Exception("This class is a singleton!")
        self.config = AppConfig()

    def set_language(self, lang_code: str = None):
        """
        Indstiller det aktive sprog for applikationen.
        """
        if lang_code is None:
             lang_code = self.config.get("General", "language", fallback="en")
        
        if lang_code not in LANGUAGES:
            lang_code = "en"

        LocalizationManager._current_lang = lang_code

        # Set the locale for the application
        try:
            if os.name == 'nt':
                # Windows uses language names that are different from the standard
                # We map our language codes to Windows-specific locale strings
                locale.setlocale(locale.LC_ALL, _WINDOWS_LOCALES.get(lang_code, "en_US.UTF-8"))
            else:
                # For other OS (Linux, macOS)
                locale.setlocale(locale.LC_ALL, f"{lang_code}.UTF-8")
        except locale.Error:
            logger.warning("Locale for %s not supported. Falling back to English.", lang_code)
            # Fallback to English if the system does not support the selected language
            if os.name == 'nt':
                locale.setlocale(locale.LC_ALL, "English_United States.1252")
            else:
                locale.setlocale(locale.LC_ALL, "en_US.UTF-8")

        # Set up gettext (lang_code is always valid here — validated above)
        try:
            lang = gettext.translation('messages', localedir=LOCALE_DIR, languages=[lang_code])
            lang.install()
            self._translator = lang
        except Exception as e:
            logger.error("Kunne ikke indlæse sprog '%s': %s", lang_code, e)
            self._translator = None

    @staticmethod
    def get_language_name(lang_code: str) -> str:
        """Returns the full name of a language from its code."""
        return LANGUAGES.get(lang_code, "English")

    @staticmethod
    def get_language_code(language_name: str) -> str:
        """Returns the language code from its full name."""
        # Performance: O(1) reverse lookup instead of O(n) linear scan
        return _LANGUAGE_NAMES_REVERSE.get(language_name, "en")

    @staticmethod
    def get_supported_languages() -> list[str]:
        return list(LANGUAGES.values())
