# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# esptool carries its stub flashers as 24 JSON files under
# targets/stub_flasher, and uploads one to the chip before it can read or
# write anything. hiddenimports collects modules and not data, so naming
# the modules alone produced a build that started, opened the tab, talked
# to the module - and then failed at "Uploading stub flasher", which is
# the first thing either button does for real.
_esp_datas = collect_data_files('esptool') + collect_data_files('esp_pylib')
_esp_modules = collect_submodules('esptool') + collect_submodules('esp_pylib')


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('mavjoyback.png', '.'), ('mavjoy_icon.png', '.'), ('mavjoy.ico', '.'),
           ('mavjoy_lockup_48.png', '.'), ('mavjoy_lockup_64.png', '.'), ('mavjoy_lockup_80.png', '.'), ('mavjoy_lockup_96.png', '.'), ('mavjoy_lockup_128.png', '.'),
           ('LICENSE', '.')] + _esp_datas,
    # esptool and littlefs are reached only from the TX module tab, through
    # names PyInstaller cannot see: esptool dispatches its subcommands at
    # run time, and littlefs loads a compiled extension. Without these the
    # app builds and runs, and that one tab fails at the moment it is used.
    hiddenimports=['littlefs', 'littlefs.lfs'] + _esp_modules,
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
