# TickerIcon

[![Latest Release](https://img.shields.io/github/v/release/phongpv7/tickericon)](https://github.com/phongpv7/tickericon/releases/latest)
[![License: MIT](https://img.shields.io/github/license/phongpv7/tickericon)](LICENSE)

A lightweight, open-source Windows application that embeds a live, automatically updating stock ticker directly into the system tray. 
Relies on Yahoo Finance for 1-minute updates during regular, pre-market, and after-hours trading sessions.

## Features
* **At-a-glance:** The icon displays the daily percentage change on a static background, with a thin strip along the bottom that is color-coded for market state (premarket, open, after hours, closed) and turns to the error color when data can't be fetched — in which case the icon keeps showing the last known values. Every market state has its own color scheme (positive/negative text colors and strip color), so symbols from exchanges around the world stay readable in one rotation. The strip width is configurable and can be set to 0 to disable it.
* **Detailed Tooltips:** Hover over the icon to instantly see current price, exact percentage change, day high, and day low.
* **Sleep:** Automatically suspends network requests overnight and on weekends.

## Prerequisites
* Windows 10 or 11

## Installation

1. Download `TickerIcon-Setup.exe` from the [latest release](https://github.com/phongpv7/tickericon/releases/latest).
2. Run the installer and follow the setup prompts.
3. Launch TickerIcon from the Start Menu.

If you prefer a portable copy, each release also includes `TickerIcon.zip`.

## Usage

Launch TickerIcon from the Start Menu or by opening the installed application directly. It runs silently in the background without opening a command prompt window. 

*Note: Windows hides new tray icons in the overflow menu by default. Click the up-arrow `^` on your taskbar and drag the new stock icon down to the visible portion of your taskbar so it is always visible.*

### Tray Icon Menu

Right-click the tray icon to open its context menu:

* **Settings** opens the configuration window.
* **Quit** closes the application.

A few more items appear only when they apply:

* **Next Symbol** jumps to the next enabled symbol while the tray icon rotates through several symbols; clicking the icon does the same.
* **Refresh Now** fetches fresh market data immediately; it's hidden when every symbol already refreshes in realtime.
* **Symbols** lists every configured symbol with a checkmark when there is more than one; toggle one to show or hide it. This only filters the display — data is still fetched for all configured symbols — and the last enabled symbol cannot be hidden.

### Settings

The **Settings** window is organized into three sections:

* **General** — scroll speed and display time (milliseconds, 100 ms steps), icon font size and strip width (pixels), the continuous scroll toggle, what the continuous ticker shows per symbol (percentage, price, or both), and optional daily high (↑) / low (↓) values with their change percentages.
* **Symbols** — the list of tracked ticker symbols (up to twenty).
* **Colors** — the static background and error-strip colors, plus a color scheme per market state (positive text, negative text, and strip color for Open, Premarket, After hours, and Closed). Click any swatch to open a color picker.

After you save your changes, TickerIcon reloads the configuration automatically. Configuration files from older versions are read transparently and upgraded to the new format on the next save.

## Running on Startup
Choose **Start TickerIcon when I sign in to Windows** during installation if you want it to launch automatically at sign-in. If you skipped that option, run the latest installer again and enable the startup option.

## Building a Windows Package

Python is only required when running the project from source or building release artifacts.

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