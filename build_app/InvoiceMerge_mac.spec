# -*- mode: python ; coding: utf-8 -*-
# macOS 打包专用 spec（GitHub Actions macos-latest 上执行，产物 arm64 .app）
# 用法：pyinstaller --noconfirm --clean build_app/InvoiceMerge_mac.spec

import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(ROOT, 'invoice_merge.py')],
    pathex=[],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'assets'), 'assets'),
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
    icon=os.path.join(ROOT, 'build_app', 'AppIcon.icns'),
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
app = BUNDLE(
    coll,
    name='发票合并小助手.app',
    icon=os.path.join(ROOT, 'build_app', 'AppIcon.icns'),
    bundle_identifier='local.tools.invoice-merge',
)
