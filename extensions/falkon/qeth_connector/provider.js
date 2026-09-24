// qeth — injected EIP-1193 / EIP-6963 provider for the Falkon browser.
//
// Runs in the page's MAIN world, so `window.ethereum` exists before the
// dapp's scripts run. It does NOT talk to the network directly (the
// page's CSP `connect-src` would block loopback on strict dapps).
// Instead it sends JSON-RPC over window.postMessage to the SafeJsWorld
// relay (relay.js), which forwards to the native Python bridge, which
// reaches qeth on 127.0.0.1:1248 over Qt's own network stack — outside
// Chromium's CSP and Private Network Access entirely.
//
// Live wallet events (account / chain changes made in the qeth UI) are
// surfaced by polling the locally-served, cheap eth_chainId /
// eth_accounts, since the bridge transport is plain request/response.
//
// A direct fetch() fallback is kept for the case where the relay never
// signals readiness (e.g. running outside the Falkon plugin, or on a
// permissive-CSP site) so the provider still works there.
//
// `__QETH_LOGO_DATA_URI__` is substituted with the wallet logo by the
// Python plugin at load time.

(function () {
  "use strict";
  if (window.__qethConnectorInstalled) return;
  window.__qethConnectorInstalled = true;

  // Are we inside a cross-origin sub-frame? The case that matters is a Safe
  // App running in an iframe inside the Gnosis Safe UI (app.safe.global).
  // There we must NOT present as an eager, already-connected wallet: the
  // dapp's wallet library would auto-pick our injected provider (isMetaMask +
  // an account handed back with no connect gate) ahead of its Safe connector
  // and show the signer EOA instead of the multisig. But we also can't just
  // vanish — removing window.ethereum outright breaks a dapp that touches it
  // at startup (a plain top-frame-only guard left StakeDAO with no address at
  // all). So in a sub-frame we stay PRESENT BUT INERT: window.ethereum
  // exists, but we don't claim to be MetaMask, we don't announce over
  // EIP-6963, and eth_accounts stays empty until the page explicitly calls
  // eth_requestAccounts. The dapp then reads the injected connector as
  // unauthorized and falls through to its Safe connector (which reads the
  // multisig over the Safe Apps SDK). This mirrors Frame, whose injected
  // provider is likewise not-MetaMask and unauthorized-until-approved inside
  // the frame — which is why Frame shows the multisig and we didn't.
  var IN_SUBFRAME = (window.top !== window.self);

  // Transport configuration. This file is shared BYTE-FOR-BYTE between the
  // Falkon plugin and the browser extension; the loader selects behaviour by
  // setting window.__QETH_PROVIDER_CONFIG__ before this script runs. Defaults
  // reproduce the Falkon connector exactly — poll for state over a
  // request/response bridge, keep a direct-fetch fallback, no push. The
  // extension flips these: a WebSocket-backed relay that PUSHES wallet events,
  // so no polling and no direct fallback.
  var CFG = window.__QETH_PROVIDER_CONFIG__ || {};
  try { delete window.__QETH_PROVIDER_CONFIG__; } catch (e) {}
  var DIRECT_FALLBACK = CFG.directFallback !== false;   // Falkon default: true
  var POLL = CFG.poll !== false;                        // Falkon default: true
  var PUSH = CFG.push === true;                         // Falkon default: false

  var HTTP_URL = "http://127.0.0.1:1248/";
  var LOGO = CFG.logo || "__QETH_LOGO_DATA_URI__";
  var PROVIDER_SRC = "qeth-provider";
  var RELAY_SRC = "qeth-relay";
  var POLL_MS = 4000;
  var RELAY_WAIT_MS = 2000;   // fall back to direct fetch if no relay by now

  // --- tiny event emitter (EIP-1193 surface) ---------------------------
  function Emitter() { this._h = {}; }
  Emitter.prototype.on = function (ev, fn) {
    (this._h[ev] = this._h[ev] || []).push(fn); return this;
  };
  Emitter.prototype.once = function (ev, fn) {
    var self = this;
    function g() { self.removeListener(ev, g); fn.apply(null, arguments); }
    return this.on(ev, g);
  };
  Emitter.prototype.removeListener = function (ev, fn) {
    var a = this._h[ev]; if (!a) return this;
    this._h[ev] = a.filter(function (x) { return x !== fn; }); return this;
  };
  Emitter.prototype.removeAllListeners = function (ev) {
    if (ev) delete this._h[ev]; else this._h = {}; return this;
  };
  Emitter.prototype.emit = function (ev) {
    var a = (this._h[ev] || []).slice();
    var args = Array.prototype.slice.call(arguments, 1);
    for (var i = 0; i < a.length; i++) {
      try { a[i].apply(null, args); } catch (e) { /* dapp handler threw */ }
    }
    return a.length > 0;
  };

  // --- provider --------------------------------------------------------
  function QethProvider() {
    Emitter.call(this);
    this.isQeth = true;
    // Appear as MetaMask (top frame only). Many dapps (Web3Modal / Reown
    // AppKit / wagmi's "injected" connector — e.g. Holyheld) only offer the
    // injected wallet when window.ethereum.isMetaMask is set; otherwise they
    // drop to a WalletConnect QR. Modern dapps still see us as "qeth" via the
    // EIP-6963 announcement below, so nothing that already works regresses.
    // Rabby / Coinbase set this flag the same way. In a sub-frame we
    // deliberately do NOT claim to be MetaMask — see IN_SUBFRAME above.
    this.isMetaMask = !IN_SUBFRAME;
    // Minimal slice of MetaMask's "experimental" API that some dapps
    // probe before they'll treat the provider as unlocked.
    this._metamask = {
      isUnlocked: function () { return Promise.resolve(true); },
    };
    this.chainId = null;
    this.networkVersion = null;
    this.selectedAddress = null;
    // Sub-frame gate (see IN_SUBFRAME): until the page explicitly connects,
    // eth_accounts reports empty so the dapp's injected connector stays
    // unauthorized and its Safe connector wins. The top frame is authorized
    // from the start — the existing no-click behaviour is unchanged.
    this._authorized = !IN_SUBFRAME;

    this._id = 1;
    this._pending = {};          // json-rpc id -> {resolve, reject}
    this._engaged = false;
    this._mode = null;           // 'relay' | 'direct' (chosen at engage)
    this._relayReady = false;
    this._outQueue = [];         // requests waiting for transport readiness
    this._connectedEmitted = false;
    this._pollTimer = null;
    this._subIds = {};           // push mode: sub_id -> sub_type (our subs)
    this._relayWaiters = [];     // whenRelay() callers waiting for "ready"
    // Hooks the Tron provider (below) sets to ride this transport's events.
    this._tronPush = null;       // (accounts) on a tronAccountsChanged push
    this._tronRefresh = null;    // () on a poll tick / transport reconnect

    this._listenRelay();
  }
  QethProvider.prototype = Object.create(Emitter.prototype);
  QethProvider.prototype.constructor = QethProvider;

  QethProvider.prototype._setChainId = function (cid) {
    if (typeof cid !== "string") return false;
    var changed = cid !== this.chainId;
    this.chainId = cid;
    this.networkVersion = String(parseInt(cid, 16));
    return changed;
  };

  // EIP-1193 core.
  QethProvider.prototype.request = function (args) {
    var self = this;
    if (!args || typeof args.method !== "string") {
      return Promise.reject(rpcError(-32600, "Invalid request: 'method' required"));
    }
    // Unauthorized sub-frame: report no account (the dapp reads this as "not
    // connected") until an explicit connect flips _authorized in the .then
    // below. Answered locally so we never even reach the wallet.
    // wallet_getPermissions is the same question in EIP-2255 clothing — a
    // library that probes with it would otherwise see eth_accounts granted and
    // auto-pick us over the page's Safe connector, the exact bug the inert
    // sub-frame mode exists to avoid.
    if (!this._authorized && (args.method === "eth_accounts"
                              || args.method === "wallet_getPermissions")) {
      return Promise.resolve([]);
    }
    this._engage();
    var payload = {
      jsonrpc: "2.0", id: this._id++,
      method: args.method, params: args.params || [],
    };
    return new Promise(function (resolve, reject) {
      self._pending[payload.id] = { resolve: resolve, reject: reject };
      self._dispatch(payload);
    }).then(function (result) {
      // Both are explicit, user-driven connects: several wallet libraries
      // (and every "switch account" button) reach for wallet_requestPermissions
      // rather than eth_requestAccounts, and that must lift the gate too.
      if (args.method === "eth_requestAccounts"
          || args.method === "wallet_requestPermissions") self._authorized = true;
      self._absorb(args.method, args.params, result);
      return result;
    });
  };

  // Route a payload over the active transport (or queue until ready).
  QethProvider.prototype._dispatch = function (payload) {
    if (this._mode === "relay" && this._relayReady) {
      this._postToRelay("data", JSON.stringify(payload));
    } else if (this._mode === "direct") {
      this._directSend(payload);
    } else {
      this._outQueue.push(payload);   // transport not chosen yet
    }
  };

  QethProvider.prototype._flushQueue = function () {
    var q = this._outQueue; this._outQueue = [];
    for (var i = 0; i < q.length; i++) this._dispatch(q[i]);
  };

  // Resolve/reject the matching request from a JSON-RPC response.
  QethProvider.prototype._onResponse = function (env) {
    // Server-pushed eth_subscription notification (push mode) — it carries no
    // id, so it must be handled before the id guard below drops it.
    if (env && env.method === "eth_subscription") { this._onSubPush(env.params); return; }
    if (!env || env.id == null) return;
    var p = this._pending[env.id];
    if (!p) return;
    delete this._pending[env.id];
    if (env.error) p.reject(rpcError(env.error.code, env.error.message));
    else p.resolve(env.result);
  };

  QethProvider.prototype._absorb = function (method, params, result) {
    if (method === "eth_chainId") this._setChainId(result);
    else if (method === "eth_accounts" || method === "eth_requestAccounts") {
      this.selectedAddress = (result && result[0]) || null;
    } else if (method === "wallet_switchEthereumChain" && params && params[0]) {
      try {
        if (this._setChainId(params[0].chainId)) this.emit("chainChanged", this.chainId);
      } catch (e) {}
    } else if (method === "wallet_requestPermissions"
               || method === "wallet_getPermissions") {
      var permitted = firstPermittedAccount(result);
      if (permitted) this.selectedAddress = permitted;
    } else if (method === "eth_subscribe" && result && params && params[0]) {
      // Remember our own wallet-event subscriptions so _onSubPush can map a
      // pushed notification's id back to its type (push mode only).
      this._subIds[result] = params[0];
    }
    this._markConnected();
  };

  QethProvider.prototype._markConnected = function () {
    if (!this._connectedEmitted) {
      this._connectedEmitted = true;
      this.emit("connect", { chainId: this.chainId });
    }
  };

  // --- relay transport (postMessage <-> SafeJsWorld) -------------------
  QethProvider.prototype._listenRelay = function () {
    var self = this;
    window.addEventListener("message", function (e) {
      if (e.source !== window) return;
      var d = e.data;
      if (!d || d.source !== RELAY_SRC) return;
      if (d.kind === "ready") { self._onRelayReady(); }
      else if (d.kind === "data") {
        var env; try { env = JSON.parse(d.data); } catch (e2) { return; }
        self._onResponse(env);
      }
      else if (d.kind === "event") {
        // Push transport (extension): the background reports the WebSocket to
        // the wallet coming up / going down. Falkon's relay never sends this.
        if (d.event === "connect") self._onTransportUp();
        else if (d.event === "close") self._onTransportDown();
      }
    });
  };
  QethProvider.prototype._postToRelay = function (kind, data) {
    window.postMessage({ source: PROVIDER_SRC, kind: kind, data: data }, "*");
  };
  QethProvider.prototype._onRelayReady = function () {
    if (this._mode === "direct") return;   // already committed to fallback
    this._relayReady = true;
    if (this._mode !== "relay") { this._mode = "relay"; this._flushQueue(); this._startPolling(); }
    var w = this._relayWaiters; this._relayWaiters = [];
    for (var i = 0; i < w.length; i++) w[i].resolve();
  };
  // Resolves once the privileged relay is up — for what only it can do
  // (loading TronWeb into this frame). Rejects if we fell back to direct
  // fetch: there's no relay to ask then.
  QethProvider.prototype.whenRelay = function () {
    var self = this;
    this._engage();
    if (this._relayReady) return Promise.resolve();
    if (this._mode === "direct") {
      return Promise.reject(rpcError(4900, "qeth's browser connector isn't active in this page"));
    }
    return new Promise(function (resolve, reject) {
      self._relayWaiters.push({ resolve: resolve, reject: reject });
    });
  };

  // --- direct transport (fallback: page-context fetch) -----------------
  QethProvider.prototype._directSend = function (payload) {
    var self = this;
    fetch(HTTP_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json(); })
      .then(function (env) { self._onResponse(env); })
      .catch(function (err) {
        self._onResponse({ id: payload.id,
          error: { code: -32603, message: String(err && err.message || err) } });
      });
  };

  QethProvider.prototype._engage = function () {
    if (this._engaged) return;
    this._engaged = true;
    this._postToRelay("hello");           // ask the relay to announce itself
    var self = this;
    // If no relay answers shortly, commit to the direct fetch fallback.
    // Disabled in push mode (the extension): its relay is always present, so
    // the queue simply waits for "ready" rather than racing a fetch fallback.
    if (DIRECT_FALLBACK) {
      setTimeout(function () {
        if (self._mode == null) {
          self._mode = "direct"; self._flushQueue(); self._startPolling();
          var w = self._relayWaiters; self._relayWaiters = [];
          for (var i = 0; i < w.length; i++) {
            w[i].reject(rpcError(4900, "qeth's browser connector isn't active in this page"));
          }
        }
      }, RELAY_WAIT_MS);
    }
  };

  // Re-read chain + account and emit on change. Shared by the poll tick and
  // by the push transport's reconnect (_onTransportUp).
  QethProvider.prototype._refreshState = function () {
    var self = this;
    // Snapshot BEFORE the request: request() runs _absorb() in its own
    // .then, which updates self.chainId / self.selectedAddress before
    // the callbacks below — so comparing against self.* here would
    // always see "no change" and never emit. Compare against the
    // captured previous value instead.
    var prevChain = this.chainId;
    this.request({ method: "eth_chainId" }).then(function (cid) {
      if (cid !== prevChain) self.emit("chainChanged", cid);
    }).catch(function () { self._onPollError(); });
    var prevAccount = this.selectedAddress;
    this.request({ method: "eth_accounts" }).then(function (accs) {
      var next = (accs && accs[0]) || null;
      if (next !== prevAccount) self.emit("accountsChanged", accs || []);
    }).catch(function () {});
    if (this._tronRefresh) this._tronRefresh();
  };

  // --- event polling (no push subscription over this transport) --------
  QethProvider.prototype._startPolling = function () {
    if (!POLL) return;               // push mode gets events over the socket
    if (this._pollTimer) return;
    var self = this;
    var tick = function () {
      if (typeof document !== "undefined" && document.hidden) return;  // idle when tab hidden
      self._refreshState();
    };
    this._pollTimer = setInterval(tick, POLL_MS);
  };
  QethProvider.prototype._onPollError = function () {
    if (this._connectedEmitted) {
      this._connectedEmitted = false;
      this.emit("disconnect", rpcError(4900, "qeth unreachable"));
    }
  };

  // --- push transport (WebSocket-backed relay: the extension) ----------
  // The background reports the wallet socket going up/down via relay "event"
  // messages. On up we (re)subscribe to wallet events — each subscribe
  // carries this frame's origin, so the server scopes pushes per dapp — and
  // refresh state. On down we fail everything in flight with 4900.
  QethProvider.prototype._onTransportUp = function () {
    var noop = function () {};
    if (PUSH) {
      this._subIds = {};
      this.request({ method: "eth_subscribe", params: ["chainChanged"] }).catch(noop);
      this.request({ method: "eth_subscribe", params: ["accountsChanged"] }).catch(noop);
      this.request({ method: "eth_subscribe", params: ["networkChanged"] }).catch(noop);
    }
    this._refreshState();
    this._markConnected();
  };
  QethProvider.prototype._onTransportDown = function () {
    this._subIds = {};
    var pend = this._pending; this._pending = {};
    for (var id in pend) {
      if (Object.prototype.hasOwnProperty.call(pend, id)) {
        try { pend[id].reject(rpcError(4900, "qeth disconnected")); } catch (e) {}
      }
    }
    this._onPollError();             // reset connected flag + emit disconnect
  };
  // A pushed eth_subscription notification: map its id back to the type we
  // subscribed to and emit the matching EIP-1193 event (deduped on value).
  QethProvider.prototype._onSubPush = function (params) {
    if (!params) return;
    var sub = this._subIds[params.subscription];
    var result = params.result;
    if (sub === "chainChanged") {
      if (this._setChainId(result)) this.emit("chainChanged", this.chainId);
    } else if (sub === "accountsChanged") {
      var next = (result && result[0]) || null;
      if (next !== this.selectedAddress) {
        this.selectedAddress = next;
        this.emit("accountsChanged", result || []);
      }
    } else if (sub === "tronAccountsChanged") {
      if (this._tronPush) this._tronPush(result || []);
    } else if (sub === "networkChanged") {
      var nv = String(result);
      if (nv !== this.networkVersion) {
        this.networkVersion = nv;
        this.emit("networkChanged", result);
      }
    } else {
      // A subscription the dapp created itself (e.g. newHeads). Surface it as
      // the EIP-1193 `message` event. Dormant today — the server doesn't
      // forward proxied-node subscriptions — but spec-correct.
      this.emit("message", { type: "eth_subscription", data: params });
    }
  };

  // --- legacy compatibility shims --------------------------------------
  QethProvider.prototype.enable = function () {
    return this.request({ method: "eth_requestAccounts" });
  };
  QethProvider.prototype.isConnected = function () { return this._connectedEmitted; };
  QethProvider.prototype.on = function (ev, fn) {
    this._engage();
    return Emitter.prototype.on.call(this, ev, fn);
  };
  QethProvider.prototype.send = function (a, b) {
    if (typeof a === "string") return this.request({ method: a, params: b || [] });
    if (typeof b === "function") return this.sendAsync(a, b);
    var method = a && a.method, result;
    switch (method) {
      case "eth_accounts": result = this.selectedAddress ? [this.selectedAddress] : []; break;
      case "eth_coinbase": result = this.selectedAddress || null; break;
      case "net_version": result = this.networkVersion; break;
      case "eth_chainId": result = this.chainId; break;
      default: throw rpcError(-32601, "qeth: synchronous send unsupported for " + method);
    }
    return { id: a.id, jsonrpc: "2.0", result: result };
  };
  QethProvider.prototype.sendAsync = function (payload, cb) {
    var self = this;
    function one(p) {
      return self.request({ method: p.method, params: p.params })
        .then(function (result) { return { id: p.id, jsonrpc: "2.0", result: result }; })
        .catch(function (err) {
          return { id: p.id, jsonrpc: "2.0",
                   error: { code: err.code || -32603, message: err.message } };
        });
    }
    if (Array.isArray(payload)) Promise.all(payload.map(one)).then(function (rs) { cb(null, rs); });
    else one(payload).then(function (r) { r.error ? cb(r.error, r) : cb(null, r); });
  };

  // --- helpers ---------------------------------------------------------
  function rpcError(code, message) {
    var e = new Error(message || "qeth error"); e.code = code; return e;
  }

  // EIP-2255: dig the address out of an eth_accounts permission's
  // restrictReturnedAccounts caveat, so a dapp that connects with
  // wallet_requestPermissions alone still populates selectedAddress (it never
  // calls eth_accounts, and would otherwise read a null address off us).
  function firstPermittedAccount(perms) {
    if (!perms || !perms.length) return null;
    for (var i = 0; i < perms.length; i++) {
      var p = perms[i];
      if (!p || p.parentCapability !== "eth_accounts") continue;
      var caveats = p.caveats || [];
      for (var j = 0; j < caveats.length; j++) {
        var c = caveats[j];
        if (c && c.type === "restrictReturnedAccounts"
            && c.value && c.value.length) return c.value[0];
      }
    }
    return null;
  }

  // --- install ---------------------------------------------------------
  var provider = new QethProvider();

  try { window.qeth = provider; } catch (e) {}
  try {
    if (!window.ethereum) {
      Object.defineProperty(window, "ethereum", {
        value: provider, configurable: true, writable: true,
      });
    }
  } catch (e) { try { window.ethereum = provider; } catch (e2) {} }

  // EIP-6963 discovery — top frame only. In a sub-frame we stay unadvertised
  // so a Safe App's wallet library discovers only its Safe connector, not us.
  if (!IN_SUBFRAME) {
    var info = Object.freeze({
      uuid: (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : "qeth-" + Date.now() + "-" + Math.floor(Math.random() * 1e9),
      name: "qeth",
      icon: LOGO,
      rdns: "org.qeth",
    });
    var announce = function () {
      window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {
        detail: Object.freeze({ info: info, provider: provider }),
      }));
    };
    window.addEventListener("eip6963:requestProvider", announce);
    announce();
  }

  // =====================================================================
  // Tron — TIP-1193 `window.tron`, TIP-6963 discovery, and TronLink's
  // legacy `window.tronLink` / `window.tronWeb` (only where absent), backed
  // by a REAL TronWeb (the unmodified npm dist, shipped with the connector).
  //
  // Every page gets only this small stub. TronWeb itself (~1 MB) is loaded
  // into THIS frame the first time the page uses Tron — asks for accounts,
  // or touches tronWeb — by the privileged side of the connector (the
  // extension's scripting API / Falkon's native runJavaScript), so the
  // page's CSP doesn't apply and an EVM-only page never pays for it.
  //
  // The TronWeb instance is TronLink-shaped: its node provider goes through
  // qeth (tron_node — qeth's nodes, failover, no page CSP / CORS), and
  // trx.sign / signMessageV2 / _signTypedData ask qeth, which shows the
  // review dialog. Calls with the dapp's OWN private key stay local, as in
  // TronWeb. Sub-frames are inert until an explicit connect, as above.
  // =====================================================================
  var tronAuthorized = !IN_SUBFRAME;
  var tronAccount = null;         // base58 of the connected account, or null
  var tronNetwork = null;         // {chainId, fullHost, name} from qeth
  var tronLib = null;             // the TronWeb namespace (TronWeb, providers…)
  var tronWeb = null;             // the instance handed to the page
  var tronSetAddress = null;      // TronWeb's own setAddress (page copy disabled)
  var tronLibLoading = null;
  var tronReady = null;

  function tronCall(method, params) {
    return provider.request({ method: method, params: params || [] });
  }

  // TronLink's window messages, for dapps that still listen for them.
  function postTronLink(action, data) {
    try {
      window.postMessage({ message: { action: action, data: data || {} },
                           isTronLink: true }, "*");
    } catch (e) {}
  }

  // Load the TronWeb library into this frame (once). The UMD bundle sets
  // window.TronWeb; we take it and put back whatever the page had there.
  // An AMD page (RequireJS: define.amd) would get it registered as a module
  // instead, so define.amd is masked for the load — define itself stays.
  // (TronWeb's generated protobuf code keeps its classes on the globals
  // TronWebProto and proto, resolved on every call — those have to stay, as
  // they do under TronLink.)
  function loadTronLib() {
    if (tronLib) return Promise.resolve(tronLib);
    if (tronLibLoading) return tronLibLoading;
    tronLibLoading = provider.whenRelay().then(function () {
      return new Promise(function (resolve, reject) {
        var prev = Object.getOwnPropertyDescriptor(window, "TronWeb");
        var amd = (typeof window.define === "function") ? window.define : null;
        var amdFlag = amd ? amd.amd : undefined;
        if (amd && amdFlag) { try { amd.amd = undefined; } catch (e) {} }
        var timer = null;
        function finish(ok, err) {
          window.removeEventListener("message", onMsg);
          clearTimeout(timer);
          if (amd && amdFlag) { try { amd.amd = amdFlag; } catch (e) {} }
          var got = window.TronWeb;
          try {
            if (prev) Object.defineProperty(window, "TronWeb", prev);
            else delete window.TronWeb;
          } catch (e) {}
          if (ok && got && typeof got.TronWeb === "function") { tronLib = got; resolve(got); }
          else reject(rpcError(4900, err || "qeth couldn't load TronWeb into this page"));
        }
        function onMsg(e) {
          if (e.source !== window) return;
          var d = e.data;
          if (!d || d.source !== RELAY_SRC || d.kind !== "tronweb") return;
          finish(!!d.ok, d.error);
        }
        window.addEventListener("message", onMsg);
        timer = setTimeout(function () { finish(false, "timed out loading TronWeb"); }, 20000);
        provider._postToRelay("tronweb");
      });
    }).catch(function (err) { tronLibLoading = null; throw err; });
    return tronLibLoading;
  }

  function hexOf(bytes) {
    var out = "0x";
    for (var i = 0; i < bytes.length; i++) out += ("0" + (bytes[i] & 0xff).toString(16)).slice(-2);
    return out;
  }

  // A signMessageV2 message → the exact bytes TronWeb hashes: a string is
  // its UTF-8, an array / Uint8Array its bytes.
  function messageHex(message) {
    if (typeof message === "string") return hexOf(new TextEncoder().encode(message));
    if (message && typeof message.length === "number") return hexOf(message);
    throw rpcError(-32602, "the message must be a string or bytes");
  }

  // Typed data for the wire: byte arrays → 0x hex, BigInts → decimal.
  function jsonable(x) {
    if (typeof x === "bigint") return x.toString();
    if (x instanceof Uint8Array) return hexOf(x);
    if (Array.isArray(x)) return x.map(jsonable);
    if (x && typeof x === "object") {
      if (typeof x.toJSON === "function") return x.toJSON();
      var out = {};
      for (var k in x) {
        if (Object.prototype.hasOwnProperty.call(x, k)) out[k] = jsonable(x[k]);
      }
      return out;
    }
    return x;
  }

  function makeTronWeb(lib, net) {
    function node() {
      var p = new lib.providers.HttpProvider(net.fullHost);
      p.request = function (url, payload, method) {
        return tronCall("tron_node", [String(url), payload || {}, String(method || "get")]);
      };
      return p;
    }
    var tw = new lib.TronWeb({ fullNode: node(), solidityNode: node(), eventServer: node() });
    var trx = tw.trx;
    var own = {
      sign: trx.sign.bind(trx), multiSign: trx.multiSign.bind(trx),
      signMessageV2: trx.signMessageV2.bind(trx),
      _signTypedData: trx._signTypedData.bind(trx),
    };
    trx.sign = function (transaction, privateKey, useTronHeader, multisig) {
      if (privateKey) return own.sign(transaction, privateKey, useTronHeader, multisig);
      if (typeof transaction === "string") {
        // Legacy message signing. With useTronHeader=false TronWeb uses
        // Ethereum's header — i.e. an Ethereum personal_sign by the same key.
        if (useTronHeader === false) {
          return Promise.reject(rpcError(4200,
            "qeth won't sign with the Ethereum message header on Tron"));
        }
        if (!/^(0x)?[0-9a-fA-F]*$/.test(transaction)) {
          return Promise.reject(rpcError(-32602, "Expected hex message input"));
        }
        return tronCall("tron_signMessage", ["0x" + transaction.replace(/^0x/, ""), 1]);
      }
      if (multisig) {
        return Promise.reject(rpcError(4200, "qeth doesn't support Tron multi-signature"));
      }
      if (!transaction || typeof transaction !== "object") {
        return Promise.reject(rpcError(-32602, "Invalid transaction provided"));
      }
      return tronCall("tron_signTransaction", [jsonable(transaction)]).then(function (sig) {
        var signed = {};
        for (var k in transaction) {
          if (Object.prototype.hasOwnProperty.call(transaction, k)) signed[k] = transaction[k];
        }
        signed.signature = [sig];
        return signed;
      });
    };
    trx.multiSign = function (transaction, privateKey, permissionId) {
      if (privateKey) return own.multiSign(transaction, privateKey, permissionId);
      return Promise.reject(rpcError(4200, "qeth doesn't support Tron multi-signature"));
    };
    trx.signMessageV2 = function (message, privateKey) {
      if (privateKey) return own.signMessageV2(message, privateKey);
      try { return tronCall("tron_signMessage", [messageHex(message), 2]); }
      catch (e) { return Promise.reject(e); }
    };
    trx._signTypedData = trx.signTypedData = function (domain, types, value, privateKey) {
      if (privateKey) return own._signTypedData(domain, types, value, privateKey);
      return tronCall("tron_signTypedData", [jsonable(domain), jsonable(types), jsonable(value)]);
    };
    // As TronLink: the page doesn't re-point the wallet's instance.
    tronSetAddress = tw.setAddress.bind(tw);
    ["setPrivateKey", "setAddress", "setFullNode", "setSolidityNode", "setEventServer"]
      .forEach(function (m) {
        tw[m] = function () { throw new Error("qeth has disabled " + m + " on its TronWeb"); };
      });
    tw.ready = false;
    return tw;
  }

  function applyTronAccount(next) {
    if (!tronAuthorized) next = null;
    if (next === tronAccount) return;
    var had = tronAccount;
    tronAccount = next;
    if (tronWeb) {
      if (next) tronSetAddress(next);
      else tronWeb.defaultAddress = { hex: false, base58: false };
      tronWeb.ready = !!next;
    }
    tronLinkObj.ready = !!next;
    tronProvider.emit("accountsChanged", next ? [next] : []);
    if (next) {
      postTronLink("setAccount", { address: next });
      postTronLink("accountsChanged", { address: next });
    } else if (had) {
      postTronLink("disconnect", {});
    }
  }

  // Load TronWeb, build the instance, read the account — once per frame.
  function tronEnsure() {
    if (tronReady) return tronReady;
    tronReady = Promise.all([loadTronLib(), tronCall("tron_network")]).then(function (r) {
      tronNetwork = r[1];
      tronWeb = makeTronWeb(r[0], tronNetwork);
      provider._tronPush = function (accounts) { applyTronAccount((accounts && accounts[0]) || null); };
      // Poll tick (Falkon) → re-read; push transport back up (the extension)
      // → re-subscribe, the old subscription died with the socket.
      provider._tronRefresh = function () {
        if (PUSH) tronSubscribe();
        tronRefresh();
      };
      tronSubscribe();
      return tronRefresh();
    }).then(function () { return tronWeb; }, function (err) { tronReady = null; throw err; });
    return tronReady;
  }

  function tronSubscribe() {
    if (PUSH) tronCall("eth_subscribe", ["tronAccountsChanged"]).catch(function () {});
  }

  function tronRefresh() {
    if (!tronAuthorized) return Promise.resolve();
    return tronCall("tron_accounts").then(function (accs) {
      applyTronAccount((accs && accs[0]) || null);
    }, function () {});
  }

  // Page-initiated connect (TIP-1193 eth_requestAccounts, TronLink's
  // tron_requestAccounts): lifts the sub-frame gate, like eth_requestAccounts.
  function tronConnect() {
    return tronEnsure().then(function () {
      return tronCall("tron_requestAccounts");
    }).then(function (accs) {
      var addr = (accs && accs[0]) || null;
      if (!addr) {
        throw rpcError(4001, "No Tron account is connected in qeth — pick one "
                             + "in the wallet's Tron view");
      }
      tronAuthorized = true;
      applyTronAccount(addr);
      postTronLink("connect", {});
      return addr;
    });
  }

  function tronRequest(args) {
    var method = args && args.method;
    var params = (args && args.params) || [];
    switch (method) {
      case "eth_requestAccounts":
      case "tron_requestAccounts":
        return tronConnect().then(function (a) { return [a]; });
      case "eth_accounts":
      case "tron_accounts":
        if (!tronAuthorized) return Promise.resolve([]);
        return tronEnsure().then(function () { return tronAccount ? [tronAccount] : []; });
      case "eth_chainId":
      case "tron_chainId":
        return tronEnsure().then(function () { return tronNetwork.chainId; });
      case "wallet_switchEthereumChain":
        return tronEnsure().then(function () {
          var want = params[0] && params[0].chainId;
          if (want && parseInt(want, 16) === parseInt(tronNetwork.chainId, 16)) return null;
          throw rpcError(4902, "qeth has no such Tron network");
        });
      default:
        return Promise.reject(rpcError(4200, "qeth: " + method + " isn't supported on Tron"));
    }
  }

  // window.tron (TIP-1193). isTronLink: the TronWallet adapter (and most
  // Tron dapps) only use window.tron when it's set — cf. isMetaMask. Not in
  // a sub-frame (the inert rule).
  function TronProvider() {
    Emitter.call(this);
    this.isQeth = true;
    this.isTronLink = !IN_SUBFRAME;
  }
  TronProvider.prototype = Object.create(Emitter.prototype);
  TronProvider.prototype.constructor = TronProvider;
  TronProvider.prototype.request = function (args) { return tronRequest(args); };
  var tronProvider = new TronProvider();

  // Touching the instance is "using Tron": start loading it. Until it's
  // there, TronLink's own not-ready value (false). Once only: a dapp polling
  // window.tronWeb while the load fails (qeth not running) would otherwise
  // retry on every read — after a failure only an explicit request retries.
  var tronAutoLoaded = false;
  function tronWebNow() {
    if (!tronAutoLoaded) {
      tronAutoLoaded = true;
      tronEnsure().catch(function () {});
    }
    return tronWeb || false;
  }
  Object.defineProperty(tronProvider, "tronWeb", {
    get: tronWebNow, enumerable: true, configurable: true,
  });

  // window.tronLink — TronLink's legacy surface: request answers with
  // {code, message} objects instead of rejecting.
  var tronLinkObj = { ready: false, isQeth: true, sunWeb: false };
  tronLinkObj.request = function (args) {
    var method = args && args.method;
    if (method === "tron_requestAccounts" || method === "eth_requestAccounts") {
      return tronConnect().then(function () {
        return { code: 200, message: "The site is already in the whitelist" };
      }, function (err) {
        return { code: 4001, message: (err && err.message) || "User rejected" };
      });
    }
    return tronRequest(args);
  };
  Object.defineProperty(tronLinkObj, "tronWeb", {
    get: tronWebNow, enumerable: true, configurable: true,
  });

  // Install — never over another wallet's (or the page's own) globals.
  function installGlobal(name, desc) {
    if (name in window) return;
    try {
      desc.configurable = true;
      desc.enumerable = true;
      Object.defineProperty(window, name, desc);
    } catch (e) {}
  }
  installGlobal("tron", { value: tronProvider, writable: true });
  installGlobal("tronLink", { value: tronLinkObj, writable: true });
  installGlobal("tronWeb", {
    get: function () { var tw = tronWebNow(); return tw || undefined; },
    // A later assignment (another wallet injecting after us) simply wins.
    set: function (v) {
      Object.defineProperty(window, "tronWeb", {
        value: v, writable: true, configurable: true, enumerable: true,
      });
    },
  });

  // TIP-6963 discovery (TRON's EIP-6963) — top frame only, as above. Plus
  // TronLink's "injected" event, which some dapps wait for.
  if (!IN_SUBFRAME) {
    var tronInfo = Object.freeze({
      uuid: (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : "qeth-tron-" + Date.now() + "-" + Math.floor(Math.random() * 1e9),
      name: "qeth",
      icon: LOGO,
      rdns: "org.qeth",
    });
    var tronAnnounce = function () {
      window.dispatchEvent(new CustomEvent("TIP6963:announceProvider", {
        detail: Object.freeze({ info: tronInfo, provider: tronProvider }),
      }));
    };
    window.addEventListener("TIP6963:requestProvider", tronAnnounce);
    tronAnnounce();
    var initialized = function () {
      if (window.tronLink === tronLinkObj) {
        window.dispatchEvent(new Event("tronLink#initialized"));
      }
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initialized, { once: true });
    } else {
      setTimeout(initialized, 0);
    }
  }
})();
