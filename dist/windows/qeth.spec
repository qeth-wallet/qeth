"""PyInstaller recipe for the Windows build — a onedir folder, not onefile.

Onefile unpacks the whole runtime to %TEMP% on every launch: slow to start,
and the self-extracting bootloader is a classic antivirus false positive.

No "verify" variant: Helios publishes no Windows release, so this build is the
plain one (verified previews fall back to unverified, as on any install
without a helios binary).

libusb for Trezor needs nothing here: python-libusb1's Windows wheel ships
libusb-1.0.dll inside the usb1 package along with its own PyInstaller hook.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

repo_root = Path(SPECPATH).parent.parent
windows_dir = repo_root / "dist" / "windows"

analysis = Analysis(
    [str(windows_dir / "qeth_launcher.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=collect_data_files("qeth"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
archive = PYZ(analysis.pure)

executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="qeth",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(windows_dir / "qeth.ico"),
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="qeth-windows",
)
