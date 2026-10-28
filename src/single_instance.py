import ctypes
import sys

if sys.platform == 'win32':
    from ctypes import wintypes

    # Private DLL handles, so these argtypes never clash with other modules'
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _user32 = ctypes.WinDLL('user32')

    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
else:  # the check is Windows-only
    _kernel32 = _user32 = None

# CreateMutexW's error when another process already holds the name
_ERROR_ALREADY_EXISTS = 183

_MB_ICONINFORMATION = 0x40
_MB_SETFOREGROUND = 0x10000
# Windows may refuse a background process the foreground; topmost still shows the box
_MB_TOPMOST = 0x40000

# The claimed mutex, held for the life of the process; Windows releases it
# when the process exits, even after a crash, so it never blocks a restart
_mutex = None


def claim_single_instance(name: str) -> bool:
    """
    Claims a named mutex in this Windows session, so a second copy of the
    app can tell that one is already running.

    Args:
        name (str): The mutex name, unique to the app.

    Returns:
        bool: False when another running instance holds the mutex; True
            otherwise, including off Windows and when the mutex can't be
            created at all, so the check itself never blocks startup.
    """
    global _mutex
    if _kernel32 is None:
        return True
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _mutex = handle
    return True


def show_message(title: str, text: str) -> None:
    """
    Shows an information message box and waits for it to be closed; does
    nothing off Windows.

    Args:
        title (str): The window title.
        text (str): The message.
    """
    if _user32 is not None:
        _user32.MessageBoxW(None, text, title, _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)
