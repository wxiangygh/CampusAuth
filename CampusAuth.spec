# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('settings.html', '.'),
        ('app.ico', '.'),
        ('frontend/dist', 'frontend/dist'),
        ('config', 'config'),
    ],
    hiddenimports=[
        'warp_exclusion', 'traffic_monitor',
        'dns', 'dns.resolver', 'dns.exception', 'dns.rdatatype', 'dns.rdataclass', 'dns.rcode',
        'core', 'core.state', 'core.command', 'core.webview', 'core.network',
        'core.warp_manager', 'core.auth', 'core.startup',
        'core.config', 'core.secrets', 'core.app_state', 'core.workflow', 'core.auth_workflow', 'core.status',
        'core.updater', 'core.version',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 以下包并非本应用依赖（源码无引用、不在 requirements，历史发布产物也不含它们）；
    # 环境中若安装了会被依赖分析连带打包（如 numpy 的 ~24MB OpenBLAS、cryptography 的 ~9MB _rust），
    # 显式排除以保持产物体积
    excludes=['numpy', 'cryptography', 'httpx', 'anyio', 'sortedcontainers'],
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
    icon='app.ico',
)
