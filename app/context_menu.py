import sys
import winreg

_MENU_LABEL = "Flet med UniteDocs"
_SHELL_KEY = r"shell\UniteDocs"

_SUPPORTED_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"]

# Only meaningful when running as a compiled exe
IS_FROZEN = getattr(sys, "frozen", False)


def _menu_key(ext: str) -> str:
    return rf"Software\Classes\SystemFileAssociations\{ext}\{_SHELL_KEY}"


def _cmd_key(ext: str) -> str:
    return _menu_key(ext) + r"\command"


def is_registered() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _menu_key(".pdf")):
            return True
    except FileNotFoundError:
        return False


def register() -> None:
    exe = sys.executable
    for ext in _SUPPORTED_EXTENSIONS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _menu_key(ext)) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, _MENU_LABEL)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, exe)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _cmd_key(ext)) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')


def unregister() -> None:
    for ext in _SUPPORTED_EXTENSIONS:
        for key_path in (_cmd_key(ext), _menu_key(ext)):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            except FileNotFoundError:
                pass
