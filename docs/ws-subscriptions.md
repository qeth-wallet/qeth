# qeth — WebSocket live updates (design note)

**Status:** **implemented** — on by default (`QETH_LIVE_WS=0` to disable).
Modules: `async_chain.py` (async transport), `live_watcher.py` (the watcher,
pending-tx probe, and Transfer-log handling), wired through
`plugins/transactions.py` (owns the watcher; feeds chains/pending/account
providers; relays to tokens) and `plugins/tokens.py` (live balances + sweep
throttle). This note is the design rationale; the code is the source of
truth. Pairs with `ARCHITECTURE.md`; `CLAUDE.md` covers the web3/transport
traps it respects.

**Deltas from this sketch as built:**
- The receipt/nonce probe rides the **same** ws connection that pushed the
  head (raw `make_request`), not a separate async-http client — simpler, and
  reconnect + the legacy timer cover resilience.
- newHeads block numbers are **provider-inconsistent** (hex `str` from DRPC,
  `int` from publicnode) — normalised in `live_watcher._to_int`. Caught a
  flapping bug; the same care applies to any hex log field.
- The 60 s balance sweep **slows to 5 min** when ws is live (still covers
  native + is the floor), rather than being removed.
- `Chain.ws_url` carries validated explicit endpoints (§8); the picker ws
  field is deferred (derivation/inheritance covers real cases).
- **Still future work:** native-via-newHeads (§3a) and a unified async
  query path — the probe stays on the ws/raw path for now.

The goal: stop *polling* the chain on wall-clock timers and instead **react
to what actually happened** — a new block, a transfer touching one of our
addresses. Real-time confirmations and balance updates, and materially less
RPC traffic (no work between events).

---

## 1. What polls today (what this replaces)

| Subsystem | Mechanism | Cadence |
|---|---|---|
| Token balances + discovery | `TokensPlugin._refresh_timer` → `_on_refresh_tick` | `REFRESH_INTERVAL_MS` = 60 s (`tokens.py:241`) |
| Pending-tx confirmation | `PendingTxWatcher._timer` → `_tick` → `PendingProbeWorker` | per-tick `eth_getTransactionReceipt` (`transactions.py:260`) |
| Current-account nonce | `_nonce_timer` → `NonceCheckWorker` | periodic `eth_getTransactionCount` (`transactions.py:762`) |

Every one of these fires on a timer regardless of whether anything changed.
On a busy fleet of accounts that's a steady stream of `eth_call` /
`getReceipt` / `getBalance` to the RPC for no new information.

---

## 2. The model

- **WebSocket is the live channel; HTTP is the floor.** We never *require*
  ws. Where a chain has a working ws endpoint we subscribe; where it
  doesn't, the same logic runs on an async poll. Behaviour is identical
  either way — ws just changes *when* we're woken, not *what we do*.

- **`AsyncWeb3` is the transport abstraction.** The provider is the only
  thing chosen by URL scheme; every `await w3.eth.*` call is identical over
  ws and http. This is the reason for going async: one query path, two
  transports.

  ```python
  provider = (WebSocketProvider(url) if url.startswith("ws")
              else AsyncHTTPProvider(url))
  async with AsyncWeb3(provider) as w3:
      ...                       # subscribe (ws) OR poll-loop (http)
      await self._on_block(w3)  # identical downstream code
  ```

- **Subscriptions are triggers, balances/receipts are authoritative.** A
  pushed log/header tells us *something changed near us*; we then make one
  targeted read (`getBalance` / `getReceipt` / `balanceOf`) to get the
  truth. We never trust a log's `value` for accounting — see reorgs (§5).

---

## 3. The two subscriptions

### 3a. `newHeads` → confirmations, native balance, block clock

Subscribe once per chain. Each pushed header drives:

- **Pending-tx confirmation.** For each pending (qeth-broadcast) tx on this
  chain, one `getTransactionReceipt`. Replaces `PendingProbeWorker`'s timer
  with a block event — fewer calls (one per ~block, not per N seconds),
  confirmation latency ≈ one round-trip. *The nonce-spent → `dropped`
  detection and the idempotent rebroadcast still belong here* — port them
  into the async check (`newHeads` has no "tx dropped" signal).

- **Native balance.** ETH/XDAI receipts emit no logs, so the §3b transfer
  subscription can't see them. Re-read `getBalance(account)` on new heads
  (every block, or every Nth) — this is the only thing native polling is
  for.

- **Block-number display / "as of block N"** — free side benefit.

### 3b. `logs` (ERC-20 `Transfer` touching the account) → token balances + discovery

This is the big RPC-load win: stop the 60 s balance sweep, react to actual
transfers. `Transfer(address indexed from, address indexed to, uint256)` →
`topic0 = TRANSFER_TOPIC0` (`tx_activity.py:56`), `topic1 = from`,
`topic2 = to` (each 32-byte left-padded). You can't OR across topic
positions in one filter, so **two subscriptions** per (account, chain), no
`address` filter (any token contract):

```python
PADDED = "0x" + "00"*12 + account[2:].lower()
incoming = {"topics": [TRANSFER_TOPIC0, None, PADDED]}   # to = account
outgoing = {"topics": [TRANSFER_TOPIC0, PADDED, None]}   # from = account
```

On a pushed log:

- `log["address"]` is the token → re-read `balanceOf(token, account)` (one
  call) and update that row. *One* targeted read per actual movement, vs the
  whole multicall sweep every 60 s.
- A `to = account` log for a token **not in the list** is **discovery** — a
  received token shows up live, no Blockscout poll needed.
- Outgoing logs catch balance drops the user didn't initiate directly (a DEX
  pulling an approved token, etc.).

Native (§3a) + tokens (§3b) together cover every balance change; the 60 s
sweep becomes a slow safety net (or goes away).

---

## 4. HTTP fallback (same code, poll trigger)

When the chain's URL is http (or ws connect/subscribe fails), the *same*
async watcher runs poll loops instead of subscriptions — the downstream
`_on_block` / `_on_transfer` handlers are unchanged:

| Live (ws) | Fallback (http) |
|---|---|
| `subscribe("newHeads")` | `getBlockNumber()` every N s; act on change |
| `subscribe("logs", filter)` | `getLogs(filter, fromBlock=last)` every N s |

So "RPC doesn't support ws" is a non-event: it degrades to the polling we do
today, expressed in the same async code.

---

## 5. Reorg safety

`logs` subscriptions re-emit removed logs with `"removed": true` on a reorg.
We don't account from log values, so the rule is simple: **any log (added or
removed) → re-read the authoritative balance/receipt.** A reorg just causes a
redundant re-read, never a wrong balance. `newHeads` confirmations should
respect the wallet's existing confirmation depth before flipping a tx to
final.

---

## 6. The async subsystem

qeth's main loop is Qt (sync); `asyncio` needs its own loop. So: a dedicated
`QThread` that runs `asyncio.run(...)`, owning **one `AsyncWeb3` connection
per active chain** (a chain is "active" if it's the current view or has a
pending tx). Results come back as **queued Qt signals**:

```
confirmed(chain, hash, receipt)   -> existing _on_receipt_confirmed (unchanged)
balance_dirty(chain, account, token) -> targeted balanceOf re-read
native_dirty(chain, account)      -> getBalance re-read
head(chain, number)               -> block display / refresh hooks
link_state(chain, bool)           -> pause/resume the legacy timers
```

Routing `confirmed` / balance updates into the *existing* slots means almost
no new UI code — the watcher is a faster *source* for flows that already
exist.

---

## 7. Plumbing that does NOT carry over from the sync side

`AsyncWeb3` unifies `w3.eth.*`, **not** the transport plumbing we hardened on
`HTTPProvider`. The async-http path needs its own copy of:

- **The `qeth/<version>` User-Agent** — DRPC's Cloudflare 403s default UAs
  (`chain.py` `_build_session`). `AsyncHTTPProvider` uses `aiohttp`; set it
  via its request headers. See [[reference_web3_checksum_addr]]-adjacent
  CLAUDE.md note on the DRPC UA.
- **Multi-RPC failover** — `_failover_provider` (`chain.py`) rotates
  endpoints on transport errors and is wrapped around the *sync* provider.
  An async equivalent is needed, since a confirmation watcher chasing a
  receipt wants the same resilience.
- **`ExtraDataToPOAMiddleware`** for PoA chains (BSC/Polygon), injected on
  the sync `Web3` today.
- **Checksumming** addresses before web3 (lowercase → `InvalidAddress`),
  same trap as the sync side.

This is the real cost of (a): a second, async copy of connection setup. The
payoff is that it then becomes the shared path if/when more of qeth goes
async.

---

## 8. ws URL resolution

Add `Chain.ws_url: str = ""`, resolved in order:

1. explicit override (a ws field in the RPC dialog),
2. the `chainid.network` `wss://` entries — **we currently drop these**
   (`chainlist.py` filters to http/https); stop dropping them and probe them
   (connect + one `eth_subscribe`, like the existing `eth_chainId` probe, so
   the picker can label which endpoints have working ws),
3. derived guess `https://host → wss://host` (works for DRPC/publicnode; not
   universal — only a last resort).

No ws_url resolves → §4 fallback, i.e. today's behaviour.

---

## 9. Lifecycle

- **Re-subscribe on (account, chain) change** — the `logs` filters are
  account-specific; the wallet switching account/chain tears down and
  rebuilds them. `newHeads` is per-chain and can persist while a chain stays
  active.
- **Reconnect** with backoff; `WebSocketProvider` auto-reconnects, but wrap
  it so a hard failure flips `link_state(False)` and the legacy timers
  resume.
- **Clean shutdown is the fiddly part** — stop the asyncio loop from Qt via
  `loop.call_soon_threadsafe(...)`, close the sockets, `join()` on app close.
  Get this wrong and you get "QThread destroyed while running".

---

## 10. Phasing

1. **`newHeads` + pending-tx confirmation**, ws-only with the http poll as
   fallback. Smallest slice; proves the async subsystem + shutdown.
2. **`logs` Transfer subscription** → live token balances + discovery; drop
   the 60 s balance sweep to a slow safety net.
3. **Native via `newHeads`**, then retire the nonce timer (the nonce check
   folds into the confirmation path).
4. Optional: more of qeth onto `AsyncWeb3` once the plumbing (UA, failover,
   POA, checksum) exists in async form.

Each phase is independently shippable behind an off-by-default flag, with the
existing timers as the always-present floor.

---

## 11. Risks / open questions

- **asyncio-in-QThread shutdown** correctness (the recurring "destroyed while
  running" trap).
- **`WebSocketProvider` maturity** — it's relatively new in web3.py 7.x;
  pin the exact subscribe / `process_subscriptions` API against the locked
  version before relying on it.
- **Per-chain connection count** — bounded by (current chain ∪ chains with
  pending txs); typically 1–2.
- **Public ws endpoints** are flakier / more rate-limited than http for many
  providers; the http floor matters.
- **Duplicate plumbing drift** (§7) — the async UA/failover must not diverge
  from the sync ones.
