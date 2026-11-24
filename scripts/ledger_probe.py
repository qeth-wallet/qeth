#!/usr/bin/env python3
"""Real-Ledger check of qeth's own client over USB (docs/ledger-clear-signing.md).

Read-only by default. It lists every connected Ledger and shows, for each one,
the model, the Ethereum app's version and settings, and the first account's
address. Nothing is signed and nothing goes over the network.

``--sign`` also signs a fixed text message (personal_sign) on each device. You
confirm it on the device's screen, and the script checks that the signature
recovers to that device's address. No funds move either way.

    uv run python scripts/ledger_probe.py [--sign] [--display] [--path PATH]

Open the Ethereum app on the device(s) first, and quit Ledger Live.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eth_utils import keccak

from qeth.ledger_app import (
    EthApp,
    LedgerDevice,
    LedgerError,
    connect,
    list_devices,
    recover_parity,
)

MESSAGE = b"qeth Ledger USB check"
_MODEL_NAMES = {"nanos": "Nano S", "nanox": "Nano X", "nanosp": "Nano S Plus",
                "stax": "Stax", "flex": "Flex", "apex": "Apex",
                "speculos": "Speculos emulator"}


def _check(device: LedgerDevice, path: str, *, sign: bool, display: bool) -> None:
    with connect(device) as transport:
        app = EthApp(transport)
        config = app.get_configuration()
        print("  Ethereum app " + ".".join(map(str, config.version)))
        print("  blind signing: " + ("on" if config.blind_signing_enabled else "off"))
        print("  token info: " + ("accepted (clear token transfers work)"
                                  if config.wants_token_info else "not accepted"))
        if display:
            print("  check the address on the device's screen and approve it…")
        address = app.get_address(path, display=display)
        print(f"  {path} → {address}")
        if not sign:
            return
        print(f'  personal_sign: confirm "{MESSAGE.decode()}" on the device…')
        raw = app.sign_personal_message(path, MESSAGE)
    prefix = b"\x19Ethereum Signed Message:\n" + str(len(MESSAGE)).encode()
    recover_parity(keccak(prefix + MESSAGE), raw.r, raw.s, address)
    print(f"  signature OK: it recovers to {address}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sign", action="store_true",
                        help="also sign a text message on each device")
    parser.add_argument("--display", action="store_true",
                        help="show the address on the device to compare")
    parser.add_argument("--path", default="44'/60'/0'/0/0",
                        help="derivation path (default: the first account)")
    args = parser.parse_args()

    devices = list_devices()
    if not devices:
        print("No Ledger found. Plug it in over USB and unlock it. On Linux, "
              "check that Ledger's udev rules are installed.")
        return 2
    failed = 0
    for n, device in enumerate(devices, 1):
        name = _MODEL_NAMES.get(device.model, device.model)
        print(f"Ledger {n} of {len(devices)}: {name} "
              f"(USB product id 0x{device.product_id:04x})")
        try:
            _check(device, args.path, sign=args.sign, display=args.display)
        except LedgerError as e:
            failed += 1
            print(f"  FAILED: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
