import configparser
from . import utils

class AppConfig:
    # Performance: singleton avoids repeated disk reads when AppConfig() is
    # instantiated multiple times (e.g. LocalizationManager + PDFTool both call it).
    _instance: "AppConfig | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.config = configparser.ConfigParser()
        # Store config.ini in same directory as password cache (AppData)
        self.config_path = utils.get_app_data_path() / 'config.ini'
        self.load_config()

    def load_config(self):
        if self.config_path.exists():
            self.config.read(self.config_path)
        else:
            self.create_default_config()

    def create_default_config(self):
        self.config['General'] = {'theme': 'arc'}
        self.config['Security'] = {'bruteforce_max_len': '5'}
        with open(self.config_path, 'w') as configfile:
            self.config.write(configfile)

    def get(self, section, option, fallback=None):
        return self.config.get(section, option, fallback=fallback)

    def getint(self, section, option, fallback=None):
        return self.config.getint(section, option, fallback=fallback)

    def set(self, section, option, value):
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, option, value)

    def save(self):
        with open(self.config_path, 'w') as configfile:
            self.config.write(configfile)
