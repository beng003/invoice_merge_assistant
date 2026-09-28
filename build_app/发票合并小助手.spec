# -*- mode: python ; coding: utf-8 -*-
# 本地打包用 spec（与 CI 用的 InvoiceMerge_mac.spec 同构，路径相对仓库根）

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
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='发票合并小助手',
)
app = BUNDLE(
    coll,
    name='发票合并小助手.app',
    icon=os.path.join(ROOT, 'build_app', 'AppIcon.icns'),
    bundle_identifier='local.tools.invoice-merge',
)
