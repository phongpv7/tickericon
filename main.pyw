import sys
from pathlib import Path

from src.app_identity import APP_USER_MODEL_ID, register_app_identity
from src.icon_generator import IconGenerator
from src.single_instance import claim_single_instance, show_message
from src.ticker_icon import TickerIcon
from src.trust_store import use_windows_trust_store


def _resolve_config_path() -> str:
    """Locates config.cfg next to the executable (frozen) or this script (dev)."""
    base_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    return str(base_dir / "config.cfg")


if __name__ == "__main__":
    # A second copy would draw its prices over the first one's. The
    # installer looks for the same mutex name (AppMutex) to ask for TickerIcon to be closed
    if not claim_single_instance(APP_USER_MODEL_ID):
        show_message("TickerIcon", "TickerIcon is already running.\n\nIts icon is in the notification area "
                                   "at the right end of the taskbar; click ^ there if it's hidden.")
        sys.exit()
    # Before the first fetch: behind a TLS-inspecting firewall, market data
    # requests only verify against the CAs Windows trusts
    use_windows_trust_store()
    # Before the tray icon exists, so notifications say TickerIcon, not Python
    register_app_identity(IconGenerator._resolve_app_icon_path())
    app = TickerIcon(_resolve_config_path())
    app.start()
