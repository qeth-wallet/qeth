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

  var HTTP_URL = "http://127.0.0.1:1248/";
  var LOGO = "__QETH_LOGO_DATA_URI__";
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
    this.chainId = null;
    this.networkVersion = null;
    this.selectedAddress = null;

    this._id = 1;
    this._pending = {};          // json-rpc id -> {resolve, reject}
    this._engaged = false;
    this._mode = null;           // 'relay' | 'direct' (chosen at engage)
    this._relayReady = false;
    this._outQueue = [];         // requests waiting for transport readiness
    this._connectedEmitted = false;
    this._pollTimer = null;

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
    this._engage();
    var payload = {
      jsonrpc: "2.0", id: this._id++,
      method: args.method, params: args.params || [],
    };
    return new Promise(function (resolve, reject) {
      self._pending[payload.id] = { resolve: resolve, reject: reject };
      self._dispatch(payload);
    }).then(function (result) {
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
    setTimeout(function () {
      if (self._mode == null) { self._mode = "direct"; self._flushQueue(); self._startPolling(); }
    }, RELAY_WAIT_MS);
  };

  // --- event polling (no push subscription over this transport) --------
  QethProvider.prototype._startPolling = function () {
    if (this._pollTimer) return;
    var self = this;
    var tick = function () {
      if (typeof document !== "undefined" && document.hidden) return;  // idle when tab hidden
      // Snapshot BEFORE the request: request() runs _absorb() in its own
      // .then, which updates self.chainId / self.selectedAddress before
      // the callbacks below — so comparing against self.* here would
      // always see "no change" and never emit. Compare against the
      // captured previous value instead.
      var prevChain = self.chainId;
      self.request({ method: "eth_chainId" }).then(function (cid) {
        if (cid !== prevChain) self.emit("chainChanged", cid);
      }).catch(function () { self._onPollError(); });
      var prevAccount = self.selectedAddress;
      self.request({ method: "eth_accounts" }).then(function (accs) {
        var next = (accs && accs[0]) || null;
        if (next !== prevAccount) self.emit("accountsChanged", accs || []);
      }).catch(function () {});
    };
    this._pollTimer = setInterval(tick, POLL_MS);
  };
  QethProvider.prototype._onPollError = function () {
    if (this._connectedEmitted) {
      this._connectedEmitted = false;
      this.emit("disconnect", rpcError(4900, "qeth unreachable"));
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

  // EIP-6963 discovery.
  var info = Object.freeze({
    uuid: (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : "qeth-" + Date.now() + "-" + Math.floor(Math.random() * 1e9),
    name: "qeth",
    icon: LOGO,
    rdns: "org.qeth",
  });
  function announce() {
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {
      detail: Object.freeze({ info: info, provider: provider }),
    }));
  }
  window.addEventListener("eip6963:requestProvider", announce);
  announce();
})();
