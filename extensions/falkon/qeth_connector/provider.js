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
    // connected") until an explicit eth_requestAccounts flips _authorized in
    // the .then below. Answered locally so we never even reach the wallet.
    if (!this._authorized && args.method === "eth_accounts") {
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
      if (args.method === "eth_requestAccounts") self._authorized = true;
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
        if (self._mode == null) { self._mode = "direct"; self._flushQueue(); self._startPolling(); }
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
})();
