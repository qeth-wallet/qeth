"""Tron derivation paths (BIP44 coin type 195), shared by every signer that
holds Tron accounts. Trezor Suite / TronLink / TronWeb step the address index;
Ledger Live steps the account. Account #0 is the same key under both."""

TRON_BIP44 = "44'/195'/0'/0/{i}"
TRON_LEDGER_LIVE = "44'/195'/{i}'/0/0"

PATH_SCHEMES: dict[str, str] = {
    "Tron": TRON_BIP44,
    "Tron (Ledger Live)": TRON_LEDGER_LIVE,
}
