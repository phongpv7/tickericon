# TickerIcon

[![Latest Release](https://img.shields.io/github/v/release/phongpv7/tickericon)](https://github.com/phongpv7/tickericon/releases/latest)
[![License: MIT](https://img.shields.io/github/license/phongpv7/tickericon)](LICENSE)

A lightweight Windows app that shows market prices as text right on the taskbar, next to the tray icons, rotating through your watchlist.
Prices stream in realtime: Vietnamese stocks from the VPS price board, and crypto, gold, oil, and global stocks from Binance.

## Features
* **Taskbar widget:** Each symbol shows its name, price, and daily change (e.g. `VHM 63.5 +0.79%`) in a normal-size font on a transparent background, then rotates to the next symbol. The widget hides while a full-screen app is running.
* **Vietnamese price board colors:** Ceiling (purple), up (green), reference (yellow), down (red), and floor (blue), with prices in thousands of VND like on local price boards. A price that failed to refresh turns gray.
* **Realtime Vietnamese stocks:** HOSE, HNX, and UPCoM stocks stream from the VPS price board as each match happens during trading sessions; while the stream is down, the board is polled every second instead.
* **Realtime world markets:** Crypto, gold, silver, oil, natural gas, and US and Asian stocks stream from Binance trade by trade, converted from USDT to US dollars, with the daily change measured from midnight Vietnam time.
* **Fast updates:** The taskbar widget picks up new prices four times a second.
* **Display names:** Label any symbol, e.g. `XAU:Gold`.
* **Price alerts:** A Windows notification when a symbol reaches a target price, e.g. `VHM>=65`.
* **Detailed tooltips:** Hover over the tray icon to see the shown symbol's exchange, market state, price, change, and day high and low.
* **Sleep:** A watchlist of only Vietnamese stocks pauses its requests outside trading sessions (lunch break, nights, weekends).
* **Works behind corporate firewalls:** Data requests trust the certificates installed in Windows, so TLS-inspecting proxies don't break them.

## Prerequisites
* Windows 10 or 11

## Installation

1. Download `TickerIcon-Setup.exe` from the [latest release](https://github.com/phongpv7/tickericon/releases/latest).
2. Run the installer and follow the setup prompts. The installer isn't code-signed, so Windows SmartScreen may warn about it; choose **More info → Run anyway**.
3. Launch TickerIcon from the Start Menu.

If you prefer a portable copy, each release also includes `TickerIcon.zip`.

### Running from source

Requires Python 3.12 or newer:

```text
git clone https://github.com/phongpv7/tickericon.git
cd tickericon
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw main.pyw
```

## Usage

TickerIcon runs silently in the background. Prices appear on the taskbar just left of the tray icons; the tray icon itself shows the app icon and holds the menu (Windows may tuck it under the up-arrow `^` on the taskbar).

### Symbols

Edit the two watchlists in **Settings → Symbols** (or in `config.cfg`), comma separated, up to twenty symbols in all:

* **International** (`TICKER_INTERNATIONAL=`): each asset by its Binance name. Crypto such as `BTC` or `ETH` comes from Binance's spot market. Gold `XAU`, silver `XAG`, platinum `XPT`, palladium `XPD`, `COPPER`, WTI crude `CL`, Brent crude `BZ`, `NATGAS`, and stocks and ETFs such as `AAPL`, `TSLA`, or `SPY` come from its perpetual futures, which trade around the clock, even while their home markets are closed. Prices are converted from USDT to US dollars at Binance's USDC/USDT rate.
* **Vietnam** (`TICKER_VIETNAM=`): just the stock code, e.g. `VHM` or `SHS`.
* Add `:Name` after a symbol to label it, e.g. `BTC, BZ:Brent, XAU:Gold`.

The taskbar shows the international symbols first, then the Vietnamese ones. Yahoo Finance symbols from older versions, such as `BTC-USD` or `GC=F`, still work.

### Price Alerts

List alerts in **Settings → Alerts** (or `ALERTS=` in `config.cfg`), comma separated: a tracked symbol or its display name, `>=` or `<=`, and a target price, e.g. `VHM>=65, Gold<=4000, BTC>=90000`.

* Write prices the way the taskbar shows them: Vietnamese stocks in thousands of VND (`65` means 65,000 VND).
* Each alert notifies once when its target is reached, and again only after the price has moved back and reaches it anew. An alert whose target is already reached notifies right after startup.

### Tray Icon Menu

Right-click the tray icon to open its context menu:

* **Settings** opens the configuration window.
* **Quit** closes the application.

A few more items appear only when they apply:

* **Next Symbol** jumps to the next enabled symbol while the tray icon rotates through several symbols; clicking the icon does the same.
* **Symbols** lists every configured symbol with a checkmark when there is more than one; toggle one to show or hide it. This only filters the display — data is still fetched for all configured symbols — and the last enabled symbol cannot be hidden.

### Settings

The **Settings** window is organized into four tabs, with Save and Cancel below them:

* **Symbols** — the international and Vietnamese watchlists, up to twenty symbols in all, with optional display names.
* **Alerts** — price targets that raise a Windows notification.
* **General** — scroll speed and display time (milliseconds, 100 ms steps; the display time is also how long the taskbar widget shows each symbol), icon font size and strip width (pixels), the continuous scroll and taskbar widget toggles, what the ticker shows per symbol (percentage, price, or both), and optional daily high (↑) / low (↓) values with their change percentages.
* **Colors** — the background color (or a transparent background) and error-strip color, a color scheme per market state (positive text, negative text, and strip color for Open, Premarket, After hours, and Closed), and the price limit colors for Vietnamese stocks (ceiling, reference, floor). Click any swatch to open a color picker.

With the taskbar widget turned off, the prices go back into the tray icon itself, either one symbol at a time or as a continuously scrolling ticker.

After you save your changes, TickerIcon reloads the configuration automatically. Configuration files from older versions are read transparently and upgraded to the new format on the next save.

## Running on Startup
Choose **Start TickerIcon when I sign in to Windows** during installation if you want it to launch automatically at sign-in. If you skipped that option, run the latest installer again and enable the startup option.

When running from source, put a shortcut in your Startup folder (`Win+R` → `shell:startup`) that runs `.venv\Scripts\pythonw.exe` with `main.pyw` as its argument and the project folder as its working directory.

## Building a Windows Package

Build the application on Windows because PyInstaller produces a
platform-specific executable:

```text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m PyInstaller --clean --noconfirm tickericon.spec
copy config.cfg dist\TickerIcon\config.cfg
```

The resulting `dist\TickerIcon` folder is the input to
`installer\TickerIcon.iss`. Compile that file with Inno Setup 6 to create a
per-user installer. The installer uses the icon in
`assets\tickericon.ico`, creates a Start Menu shortcut, and optionally adds
the application to Windows startup.

Publishing a GitHub Release runs the same Windows build automatically through
`.github\workflows\release.yml`. The release receives
`TickerIcon-Setup.exe` as the one-file installer and `TickerIcon.zip` as the
portable distribution.

## License
Distributed under the MIT License. See `LICENSE` for more information.
