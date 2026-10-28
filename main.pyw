import sys
from pathlib import Path

from src.app_identity import register_app_identity
from src.icon_generator import IconGenerator
from src.ticker_icon import TickerIcon
from src.trust_store import use_windows_trust_store


def _resolve_config_path() -> str:
    """Locates config.cfg next to the executable (frozen) or this script (dev)."""
    base_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    return str(base_dir / "config.cfg")


if __name__ == "__main__":
    # Before the first fetch: behind a TLS-inspecting firewall, Yahoo
    # requests only verify against the CAs Windows trusts
    use_windows_trust_store()
    # Before the tray icon exists, so notifications say TickerIcon, not Python
    register_app_identity(IconGenerator._resolve_app_icon_path())
    app = TickerIcon(_resolve_config_path())
    app.start()
