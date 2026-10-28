import ctypes
import os
import sys
from pathlib import Path

# Identifies the app to Windows, which then titles its notifications with the
# registered display name and icon instead of the host executable's ("Python")
APP_USER_MODEL_ID = 'phongpv7.TickerIcon'
APP_DISPLAY_NAME = 'TickerIcon'


def app_data_dir() -> Path:
    """
    Returns:
        Path: The per-user folder TickerIcon keeps its own files in, such as
            its settings and CA bundle, apart from the program itself; it may
            not exist yet.
    """
    return Path(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')) / 'TickerIcon'


def register_app_identity(icon_path: Path) -> None:
    """
    Gives this process its own AppUserModelID and registers that ID's display
    name and icon for the current user. Must run before the tray icon is
    created. Does nothing off Windows; failures are ignored since they only
    affect the notification header.

    Args:
        icon_path (Path): The application icon shown on notifications.
    """
    if sys.platform != 'win32':
        return
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf'Software\Classes\AppUserModelId\{APP_USER_MODEL_ID}') as key:
            winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, APP_DISPLAY_NAME)
            if icon_path.exists():
                winreg.SetValueEx(key, 'IconUri', 0, winreg.REG_SZ, str(icon_path))
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except OSError:
        pass
