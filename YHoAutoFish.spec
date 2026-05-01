# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


ROOT = Path.cwd()
from core.version import APP_NAME


def existing_data(source, target="."):
    path = ROOT / source
    if path.exists():
        return [(str(path), target)]
    return []


def safe_collect_data_files(package):
    try:
        return collect_data_files(package)
    except Exception:
        return []


def safe_copy_metadata(package):
    try:
        return copy_metadata(package)
    except Exception:
        return []


def safe_collect_dynamic_libs(package):
    try:
        return collect_dynamic_libs(package)
    except Exception:
        return []


def safe_collect_submodules(package):
    try:
        return collect_submodules(package)
    except Exception:
        return []


datas = []
datas += existing_data("assets", "assets")
datas += existing_data("异环鱼类图鉴资源", "异环鱼类图鉴资源")
datas += existing_data("ocr_models", "ocr_models")
datas += existing_data("sponsor_qr", "sponsor_qr")
datas += existing_data("logo.jpg", ".")
datas += existing_data("build_assets/logo.ico", ".")
datas += existing_data("config.json", ".")
binaries = []

hiddenimports = [
    "cnocr",
    "cnstd",
    "cv2",
    "core.admin",
    "core.dpi",
    "core.updater",
    "core.version",
    "mss",
    "onnxruntime",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtNetwork",
    "PySide6.QtWidgets",
    "pydirectinput",
    "rapidocr",
    "shiboken6",
    "win32api",
    "win32gui",
    "win32process",
]

for package in ("cnocr", "cnstd", "rapidocr", "onnxruntime"):
    datas += safe_collect_data_files(package)
    datas += safe_copy_metadata(package)
    binaries += safe_collect_dynamic_libs(package)
    hiddenimports += safe_collect_submodules(package)


a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
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
    manifest=str(ROOT / "YHoAutoFish.manifest"),
    uac_admin=True,
    version=str(ROOT / "version_info.txt"),
    icon=str(ROOT / "build_assets" / "logo.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
