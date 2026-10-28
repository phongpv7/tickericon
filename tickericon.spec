# PyInstaller spec for TickerIcon.
# Build with: pyinstaller tickericon.spec
#
# Produces a one-folder ("onedir") distribution rather than a single onefile
# exe: onefile's runtime self-extraction to a temp dir is a common trigger
# for AV/Defender false positives. config.cfg is NOT bundled (it's
# user-editable), so copy it into the output folder after packaging.

a = Analysis(
    ['main.pyw'],
    pathex=[],
    binaries=[],
    datas=[('assets/tickericon.ico', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TickerIcon',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-compressed binaries closely resemble how malware droppers pack
    # themselves, and are a common cause of AV/Defender false positives.
    upx=False,
    icon='assets/tickericon.ico',
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TickerIcon',
)
