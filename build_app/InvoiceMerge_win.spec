# -*- mode: python ; coding: utf-8 -*-
# Windows 打包专用 spec（GitHub Actions windows-latest 上执行）
# 用法：pyinstaller --noconfirm --clean build_app/发票合并小助手-win.spec

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
SRC = os.path.join(ROOT, 'src')

a = Analysis(
    [os.path.join(SRC, 'invoice_merge.py')],
    pathex=[SRC],
    binaries=[],
    datas=[
        (os.path.join(SRC, 'assets'), 'assets'),
    ],
    hiddenimports=['tkinter', 'gui_qt'],
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
    [],
    exclude_binaries=True,
    name='发票合并小助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, 'build_app', 'AppIcon.ico'),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='发票合并小助手',
)
