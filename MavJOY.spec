# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('mavjoyback.png', '.'), ('mavjoy_icon.png', '.'), ('mavjoy.ico', '.'),
           ('mavjoy_lockup_48.png', '.'), ('mavjoy_lockup_64.png', '.'), ('mavjoy_lockup_80.png', '.'), ('mavjoy_lockup_96.png', '.'), ('mavjoy_lockup_128.png', '.')],
    # esptool and littlefs are reached only from the TX module tab, through
    # names PyInstaller cannot see: esptool dispatches its subcommands at
    # run time, and littlefs loads a compiled extension. Without these the
    # app builds and runs, and that one tab fails at the moment it is used.
    hiddenimports=['esptool', 'esptool.cmds', 'esptool.targets',
                   'littlefs', 'littlefs.lfs'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MavJOY',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['mavjoy.ico'],
)
