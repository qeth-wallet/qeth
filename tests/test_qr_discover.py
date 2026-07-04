"""QR account discovery: derivation schemes (path rendering) + QRAccountWorker
(local address derivation + auto-detect), headless."""

from eth_keys import keys

from qeth.qr import derive
from qeth.qr.account import AccountKey
from qeth.qr.schemes import QR_ADDRESS_SCHEMES, components_to_path, full_path

ORIGIN = [44, True, 60, True, 0, True]   # m/44'/60'/0'
_PUB = keys.PrivateKey(b"\x37" * 32).public_key.to_compressed_bytes()
_CC = b"\x42" * 32


def _key():
    return AccountKey(pubkey=_PUB, chain_code=_CC, origin_path=ORIGIN,
                      source_fingerprint=0x12345678, parent_fingerprint=0)


def test_components_to_path():
    assert components_to_path(ORIGIN) == "m/44'/60'/0'"
    assert components_to_path([44, True, 60, True, 0, False, 1, False]) == "m/44'/60'/0/1"


def test_full_path_appends_the_scheme_suffix():
    assert full_path(ORIGIN, [0, 5]) == "m/44'/60'/0'/0/5"     # BIP44
    assert full_path(ORIGIN, [5]) == "m/44'/60'/0'/5"          # Legacy


def test_schemes_produce_distinct_suffixes():
    assert QR_ADDRESS_SCHEMES["BIP44 (…/0/i)"](3) == [0, 3]
    assert QR_ADDRESS_SCHEMES["Legacy (…/i)"](3) == [3]


def test_worker_derives_correct_addresses_and_paths(qtbot):
    from qeth.qr_discover import QRAccountWorker
    found = []
    worker = QRAccountWorker(_key(), "BIP44 (…/0/i)", count=3, chain=None)
    worker.discovered.connect(found.append)
    worker.run()   # run synchronously (no thread) — chain=None → nonces 0
    assert len(found) == 3
    for i, acct in enumerate(found):
        assert acct.index == i
        assert acct.path == f"m/44'/60'/0'/0/{i}"
        assert acct.address == derive.derive_address(_PUB, _CC, [0, i])
        assert acct.nonce == 0


def test_worker_legacy_scheme_uses_flat_suffix(qtbot):
    from qeth.qr_discover import QRAccountWorker
    found = []
    worker = QRAccountWorker(_key(), "Legacy (…/i)", count=2, chain=None)
    worker.discovered.connect(found.append)
    worker.run()
    assert [a.path for a in found] == ["m/44'/60'/0'/0", "m/44'/60'/0'/1"]


def test_worker_auto_detect_stops_after_three_unused(qtbot):
    from qeth.qr_discover import QRAccountWorker
    found = []
    worker = QRAccountWorker(_key(), "BIP44 (…/0/i)", count=0, chain=None)
    worker.discovered.connect(found.append)
    worker.run()   # chain=None → every nonce 0 → stop after 3
    assert len(found) == 3


def test_worker_unknown_scheme_fails(qtbot):
    from qeth.qr_discover import QRAccountWorker
    errors = []
    worker = QRAccountWorker(_key(), "nope", count=1, chain=None)
    worker.failed.connect(errors.append)
    worker.run()
    assert errors and "scheme" in errors[0]
