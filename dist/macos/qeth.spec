"""PyInstaller recipe for the native macOS application bundle.

Set ``QETH_BUNDLE_HELIOS=/path/to/helios`` to build the "verify" variant, which
ships the Helios light client so transaction previews run against proof-verified
state out of the box — the same opt-in the .deb/.rpm/Flatpak/AppImage builds
use. It lands beside the app's other binaries and qeth_launcher.py points
QETH_HELIOS_BIN at it. The variant is named separately so both can be built into
the same distpath; the bundle identifier is deliberately shared, since they are
alternative builds of one app (as on every other platform).
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

from qeth import __version__


repo_root = Path(SPECPATH).parent.parent
macos_dir = repo_root / "dist" / "macos"

_helios = os.environ.get("QETH_BUNDLE_HELIOS", "").strip()
if _helios and not Path(_helios).is_file():
    raise SystemExit(f"QETH_BUNDLE_HELIOS is not a file: {_helios}")
_binaries = [(_helios, ".")] if _helios else []
_name = "qeth-verify-macos" if _helios else "qeth-macos"

analysis = Analysis(
    [str(macos_dir / "qeth_launcher.py")],
    pathex=[str(repo_root)],
    binaries=_binaries,
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
    name=_name,
)
app = BUNDLE(
    collected,
    name=f"{_name}.app",
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
