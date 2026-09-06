# -*- mode: python ; coding: utf-8 -*-
"""SuperTrainer PyInstaller 打包（onedir）。

构建：.venv\Scripts\python.exe -m PyInstaller runtrainer.spec --noconfirm
产物：dist\SuperTrainer\SuperTrainer.exe
验证：dist\SuperTrainer\SuperTrainer.exe --selfcheck
"""
from PyInstaller.utils.hooks import (
    collect_data_files, collect_dynamic_libs, collect_submodules,
)

hiddenimports = []
hiddenimports += collect_submodules("webview")           # pywebview 平台后端
hiddenimports += collect_submodules("keyring.backends")  # Windows 凭据后端
hiddenimports += collect_submodules("garminconnect")     # 适配器动态导入
hiddenimports += ["tzdata"]                              # zoneinfo（Windows 无系统 tz 库）

datas = []
datas += [("web", "web")]                                # 前端（_MEIPASS/web）
datas += [("src/runtrainer/db/migrations", "runtrainer/db/migrations")]  # SQL 迁移
datas += collect_data_files("tzdata")
datas += collect_data_files("webview")
datas += collect_data_files("keyring")

binaries = collect_dynamic_libs("webview")

a = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SuperTrainer",
    icon="assets/supertrainer.ico",   # exe 资源图标（资源管理器/任务栏）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # 保留控制台便于查看日志/selfcheck；正式可改 False
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SuperTrainer",
)
