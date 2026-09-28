# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/Users/beng003/Documents/正式工作/工具/发票合并小助手/invoice_merge.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('/Users/beng003/Documents/正式工作/工具/发票合并小助手/assets', 'assets'),
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
    icon=['/Users/beng003/Documents/正式工作/工具/发票合并小助手/build_app/AppIcon.icns'],
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
    icon='/Users/beng003/Documents/正式工作/工具/发票合并小助手/build_app/AppIcon.icns',
    bundle_identifier='local.tools.invoice-merge',
)
