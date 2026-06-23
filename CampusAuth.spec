# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=[],
    datas=[('settings.html', '.'), ('warp_exclusion.html', '.'), ('traffic_monitor.html', '.'), ('traffic_flow.html', '.'), ('app.ico', '.')],
    hiddenimports=['warp_exclusion', 'traffic_monitor', 'dns', 'dns.resolver', 'dns.exception', 'dns.rdatatype', 'dns.rdataclass', 'dns.rcode'],
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
    name='CampusAuth',
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
    uac_admin=True,
)
