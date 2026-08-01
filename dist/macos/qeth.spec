"""PyInstaller recipe for the native macOS application bundle."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

from qeth import __version__


repo_root = Path(SPECPATH).parent.parent
macos_dir = repo_root / "dist" / "macos"

analysis = Analysis(
    [str(macos_dir / "qeth_launcher.py")],
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
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=str(macos_dir / "qeth.entitlements"),
)
collected = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="qeth-macos",
)
app = BUNDLE(
    collected,
    name="qeth-macos.app",
    icon=str(macos_dir / "qeth.icns"),
    bundle_identifier="io.github.michwill.qeth",
    version=__version__,
    info_plist={
        "CFBundleDisplayName": "qeth",
        "CFBundleName": "qeth",
        "NSCameraUsageDescription": (
            "qeth uses the camera to scan QR codes from air-gapped wallets."
        ),
    },
)
