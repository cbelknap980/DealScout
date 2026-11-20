/*
  github-db.js: Deal Scout's data layer for GitHub Pages.

  The page was written for the claude.ai artifact runtime, where `await claude.use("db")`
  returns a small document database. This file provides the same calls, backed by JSON
  files in the private repo cbelknap980/dealscout-data, so the page code barely changed.

  How it works
  - Your GitHub token (fine-grained, Contents read and write on dealscout-data only) is
    pasted once per device and kept in this browser's localStorage. Nothing else stores it.
  - Reading: one call lists the repo's files (git tree); only files whose content changed
    since the last read are downloaded. The page re-checks every 60 seconds while visible.
  - Writing: the page only writes the files it owns: status.json (your status per listing),
    pasted.json (links you paste) and searches.json (the search editor). Collectors write
    everything else through tools/ds.py. If a file changed underneath a write, the write is
    re-applied to the newest copy (up to 3 tries).

  Collections the page asks for, and the files behind them
    meta      rules.json (doc "rules") + architecture.json from this site (doc "architecture")
    searches  searches.json                         (read and write)
    listings  listings/*.json + pasted.json, with status.json laid over each listing
    runs      runs.json                             (read only)
    sources   sites.json                            (read only)
*/
(function () {
  "use strict";
  if (window.claude && typeof window.claude.use === "function") return;

  var DEFAULT_REPO = "cbelknap980/dealscout-data";
  var BRANCH = "main";
  var API = "https://api.github.com";
  var TOKEN_KEY = "ds.gh.token";
  var REPO_KEY = "ds.gh.repo";
  var POLL_MS = 60000;

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {} }
  function repo() { return lsGet(REPO_KEY) || DEFAULT_REPO; }

  function DbError(code, message) { var e = new Error(message || code); e.code = code; return e; }

  // ---------- JSON written the same way the collectors write it (sorted keys, 1-space indent)
  function sortDeep(v) {
    if (Array.isArray(v)) return v.map(sortDeep);
    if (v && typeof v === "object") {
      var o = {}; Object.keys(v).sort().forEach(function (k) { o[k] = sortDeep(v[k]); }); return o;
    }
    return v;
  }
  function stableJson(v) { return JSON.stringify(sortDeep(v), null, 1) + "\n"; }
  function b64(str) {
    var bytes = new TextEncoder().encode(str), bin = "", CH = 0x8000;
    for (var i = 0; i < bytes.length; i += CH) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    return btoa(bin);
  }
  function clone(v) { return v == null ? v : JSON.parse(JSON.stringify(v)); }

  // ---------- GitHub API
  function gh(method, path, body, extraHeaders) {
    var token = lsGet(TOKEN_KEY);
    var headers = { "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
    if (token) headers["Authorization"] = "Bearer " + token;
    if (extraHeaders) Object.keys(extraHeaders).forEach(function (k) { headers[k] = extraHeaders[k]; });
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(API + path, { method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
  }

  // ---------- state
  var files = {};          // path -> {sha, json}
  var treeEtag = null;
  var arch = null;         // architecture.json from this site
  var loaded = false;
  var listeners = [];      // {name, q, cb, err}
  var pollTimer = null;
  var loading = null;
  var hadErr = false;      // the last refresh failed; the next good one tells every listener, changed or not
  var writeGen = 0;        // counts the page's own writes
  var pathGen = {};        // path -> writeGen of the page's last write to it

  function fileJson(path, dflt) { var f = files[path]; return f ? f.json : dflt; }

  function loadArch() {
    return fetch("architecture.json", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { arch = j; }).catch(function () { arch = null; });
  }

  // A read that started before one of the page's own writes must not put the older copy back
  // (it made a Watch tap flip back for a minute). Paths written since the read began are left alone.
  function writtenSince(gen, p) { return (pathGen[p] || 0) > gen; }

  function refresh(force) {
    if (loading) return loading;
    var startGen = writeGen, skipped = false;
    loading = (function () {
      var path = "/repos/" + repo() + "/git/trees/" + BRANCH + "?recursive=1";
      return gh("GET", path, undefined, (!force && treeEtag) ? { "If-None-Match": treeEtag } : null).then(function (r) {
        if (r.status === 304) return false;
        if (r.status === 401) throw DbError("unauthenticated", "GitHub didn't accept the token.");
        if ((r.status === 403 && (r.headers.get("x-ratelimit-remaining") === "0" || r.headers.get("retry-after"))) || r.status === 429) throw DbError("unavailable", "GitHub's hourly limit was reached; the page tries again soon.");
        if (r.status === 403 || r.status === 404) throw DbError("no_access", "The token can't read " + repo() + ".");
        if (!r.ok) throw DbError("unavailable", "GitHub answered " + r.status + ".");
        treeEtag = r.headers.get("ETag");
        return r.json().then(function (tree) {
          var want = (tree.tree || []).filter(function (e) { return e.type === "blob" && /\.json$/.test(e.path); });
          var keep = {};
          var jobs = want.map(function (e) {
            keep[e.path] = true;
            if (files[e.path] && files[e.path].sha === e.sha) return null;
            if (writtenSince(startGen, e.path)) { skipped = true; return null; }
            return gh("GET", "/repos/" + repo() + "/git/blobs/" + e.sha, undefined, { "Accept": "application/vnd.github.raw+json" })
              .then(function (br) {
                if (!br.ok) throw DbError("unavailable", "Couldn't read " + e.path + " (" + br.status + ").");
                return br.text();
              })
              .then(function (txt) {
                if (writtenSince(startGen, e.path)) { skipped = true; return; }
                files[e.path] = { sha: e.sha, json: JSON.parse(txt) };
              });
          }).filter(Boolean);
          Object.keys(files).forEach(function (p) { if (!keep[p] && !writtenSince(startGen, p)) delete files[p]; });
          return Promise.all(jobs).then(function () { return true; });
        });
      });
    })().then(function (changed) {
      loaded = true; loading = null;
      if (skipped) treeEtag = null;   // read the tree again next time, now that the page's write has landed
      if (changed || hadErr) notifyAll();
      hadErr = false;
      return changed;
    }, function (err) {
      loading = null; hadErr = true;
      if (err && err.code === "unauthenticated") { showSetup("Your GitHub token stopped working (it may have expired). Paste a new one."); }
      listeners.forEach(function (l) { if (l.err) try { l.err(err); } catch (e) {} });
      throw err;
    });
    return loading;
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () { if (document.visibilityState === "visible") refresh(false).catch(function () {}); }, POLL_MS);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") refresh(false).catch(function () {}); });
  }

  // ---------- collections built from files
  function docsFor(name) {
    var out = [];
    if (name === "meta") {
      var rules = fileJson("rules.json", null);
      if (rules) out.push({ id: "rules", data: rules });
      if (arch) out.push({ id: "architecture", data: arch });
    } else if (name === "searches") {
      var s = fileJson("searches.json", {});
      Object.keys(s).forEach(function (id) { out.push({ id: id, data: s[id] }); });
    } else if (name === "sources") {
      var st = fileJson("sites.json", {});
      Object.keys(st).forEach(function (id) { out.push({ id: id, data: st[id] }); });
    } else if (name === "runs") {
      (fileJson("runs.json", []) || []).forEach(function (r) { out.push({ id: r.id, data: r }); });
    } else if (name === "listings") {
      var status = fileJson("status.json", {}), seen = {};
      Object.keys(files).filter(function (p) { return /^listings\/[^/]+\.json$/.test(p); }).sort().forEach(function (p) {
        var m = files[p].json || {};
        Object.keys(m).forEach(function (id) {
          var d = Object.assign({}, m[id]);
          d.status = (status[id] && status[id].status) || d.status || "new";
          seen[id] = true; out.push({ id: id, data: d });
        });
      });
      // A pasted link no collector has read yet is flagged pastedOnly, so the page keeps it out of the
      // median price (ds.py never sees pasted.json when it prices, and the two must agree).
      var pasted = fileJson("pasted.json", {});
      Object.keys(pasted).forEach(function (id) {
        if (seen[id]) return;
        var d = Object.assign({}, pasted[id]);
        d.status = (status[id] && status[id].status) || d.status || "new";
        d.pastedOnly = true;
        out.push({ id: id, data: d });
      });
    }
    return out;
  }

  function runQuery(name, q) {
    var docs = docsFor(name);
    (q.where || []).forEach(function (w) {
      docs = docs.filter(function (d) {
        var v = d.data[w.f];
        switch (w.op) {
          case "==": return v === w.v; case "!=": return v !== w.v;
          case "<": return v < w.v; case "<=": return v <= w.v; case ">": return v > w.v; case ">=": return v >= w.v;
          case "in": return Array.isArray(w.v) && w.v.indexOf(v) >= 0;
          case "array-contains": return Array.isArray(v) && v.indexOf(w.v) >= 0;
          default: return true;
        }
      });
    });
    if (q.order) {
      var f = q.order.f, dir = q.order.dir === "desc" ? -1 : 1;
      docs.sort(function (a, b) { var x = a.data[f], y = b.data[f]; return (x < y ? -1 : x > y ? 1 : 0) * dir; });
    }
    if (q.limit) docs = docs.slice(0, q.limit);
    return docs;
  }

  function snapshot(docs) {
    var ds = docs.map(function (d) { return { id: d.id, exists: true, data: function () { return clone(d.data); } }; });
    return { docs: ds, size: ds.length, empty: ds.length === 0, metadata: { fromCache: false, hasPendingWrites: false },
      forEach: function (fn) { ds.forEach(fn); } };
  }

  function notifyAll() {
    listeners.forEach(function (l) { try { l.cb(snapshot(runQuery(l.name, l.q))); } catch (e) { console.error(e); } });
  }

  // ---------- writes (page-owned files only)
  function writeFile(path, mutate, message) {
    var tries = 0;
    function attempt() {
      tries++;
      var cur = files[path];
      var next = mutate(clone(cur ? cur.json : (path === "status.json" || path === "pasted.json" || path === "searches.json") ? {} : null));
      var body = { message: message, content: b64(stableJson(next)), branch: BRANCH };
      if (cur) body.sha = cur.sha;
      return gh("PUT", "/repos/" + repo() + "/contents/" + path, body).then(function (r) {
        if (r.ok) return r.json().then(function (j) {
          files[path] = { sha: j.content && j.content.sha, json: next };
          pathGen[path] = ++writeGen;
          treeEtag = null; notifyAll();
        });
        if ((r.status === 409 || r.status === 422) && tries < 3) {
          return refresh(true).then(attempt);
        }
        if (r.status === 401) { showSetup("Your GitHub token stopped working. Paste a new one."); throw DbError("unauthenticated"); }
        if (r.status === 403 || r.status === 404) throw DbError("permission_denied", "The token can't write to " + repo() + ". Give it Contents: Read and write.");
        throw DbError("unavailable", "GitHub answered " + r.status + ".");
      });
    }
    return attempt();
  }

  function now() { return new Date().toISOString(); }

  function docRef(name, id) {
    return {
      id: id,
      get: function () {
        var d = docsFor(name).filter(function (x) { return x.id === id; })[0];
        return Promise.resolve({ id: id, exists: !!d, data: function () { return d ? clone(d.data) : undefined; } });
      },
      set: function (data, opts) {
        if (name === "searches") return writeFile("searches.json", function (m) { m[id] = (opts && opts.merge) ? Object.assign({}, m[id] || {}, data) : data; return m; }, "Page: save search " + id);
        if (name === "listings") return writeFile("pasted.json", function (m) { var d = Object.assign({}, data); delete d.status; m[id] = d; return m; }, "Page: add pasted link " + id);
        return Promise.reject(DbError("permission_denied", name + " is written by the collectors"));
      },
      update: function (patch) {
        if (name === "searches") return writeFile("searches.json", function (m) { if (!m[id]) throw DbError("not_found"); m[id] = Object.assign({}, m[id], patch); return m; }, "Page: update search " + id);
        if (name === "listings") {
          var keys = Object.keys(patch);
          if (keys.length === 1 && keys[0] === "status") {
            return writeFile("status.json", function (m) {
              if (patch.status === "new") delete m[id]; else m[id] = { status: patch.status, at: now() };
              return m;
            }, "Page: mark " + id + " " + patch.status);
          }
          var pasted = fileJson("pasted.json", {});
          if (pasted[id]) return writeFile("pasted.json", function (m) { m[id] = Object.assign({}, m[id], patch); return m; }, "Page: update pasted link " + id);
          return Promise.reject(DbError("permission_denied", "Only a listing's status can be changed from the page"));
        }
        return Promise.reject(DbError("permission_denied", name + " is written by the collectors"));
      },
      delete: function () {
        if (name === "searches") return writeFile("searches.json", function (m) { delete m[id]; return m; }, "Page: delete search " + id);
        if (name === "listings" && fileJson("pasted.json", {})[id]) return writeFile("pasted.json", function (m) { delete m[id]; return m; }, "Page: remove pasted link " + id);
        return Promise.reject(DbError("permission_denied", name + " entries can't be deleted from the page"));
      }
    };
  }

  function query(name, q) {
    q = q || {};
    return {
      where: function (f, op, v) { return query(name, Object.assign({}, q, { where: (q.where || []).concat([{ f: f, op: op, v: v }]) })); },
      orderBy: function (f, dir) { return query(name, Object.assign({}, q, { order: { f: f, dir: dir || "asc" } })); },
      limit: function (n) { return query(name, Object.assign({}, q, { limit: n })); },
      doc: function (id) { return docRef(name, id); },
      add: function (data) { var id = Math.random().toString(36).slice(2, 12); return docRef(name, id).set(data).then(function () { return docRef(name, id); }); },
      get: function () { return (loaded ? Promise.resolve() : refresh(true)).then(function () { return snapshot(runQuery(name, q)); }); },
      onSnapshot: function (cb, err) {
        var l = { name: name, q: q, cb: cb, err: err };
        listeners.push(l);
        if (loaded) setTimeout(function () { cb(snapshot(runQuery(name, q))); }, 0);
        return function () { listeners = listeners.filter(function (x) { return x !== l; }); };
      }
    };
  }

  var db = {
    collection: function (name) { return query(name); },
    doc: function (path) { var p = String(path).split("/"); return docRef(p[0], p[1]); },
    repo: repo,
    forgetToken: function () { lsSet(TOKEN_KEY, null); location.reload(); }
  };

  // ---------- one-time setup screen
  var setupResolve = null;
  function showSetup(message) {
    var el = document.getElementById("ghSetup");
    if (el) { el.hidden = false; if (message) el.querySelector(".gh-err").textContent = message; return; }
    var css = document.createElement("style");
    css.textContent =
      "#ghSetup{position:fixed;inset:0;z-index:9999;background:rgba(20,38,61,.35);display:flex;align-items:center;justify-content:center;padding:16px}" +
      "#ghSetup[hidden]{display:none}" +
      "#ghSetup .gh-card{background:#f3efe6;color:#14263d;max-width:520px;width:100%;border:1px solid #d8cfbd;border-radius:6px;padding:22px 22px 18px;font:15px/1.5 'Source Sans 3',system-ui,sans-serif;box-shadow:0 10px 30px rgba(20,38,61,.18)}" +
      "#ghSetup h2{font:600 21px/1.25 'Source Serif 4',Georgia,serif;margin:0 0 8px}" +
      "#ghSetup ol{margin:8px 0 12px 20px;padding:0}#ghSetup li{margin:3px 0}" +
      "#ghSetup a{color:#ad522b}#ghSetup code{font:13px 'IBM Plex Mono',monospace;background:#e9e2d4;padding:0 4px;border-radius:3px}" +
      "#ghSetup input{width:100%;box-sizing:border-box;font:16px 'IBM Plex Mono',monospace;padding:9px 10px;border:1px solid #c9bea8;border-radius:4px;background:#fffdf8;color:#14263d}" +
      "#ghSetup button{margin-top:10px;background:#14263d;color:#f3efe6;border:0;border-radius:4px;padding:9px 16px;font:600 14px 'Source Sans 3',system-ui,sans-serif;cursor:pointer}" +
      "#ghSetup .gh-err{color:#a33a2a;min-height:1.4em;margin-top:8px;font-size:14px}#ghSetup .gh-fine{font-size:13px;color:#4c5b6e;margin-top:6px}";
    document.head.appendChild(css);
    el = document.createElement("div");
    el.id = "ghSetup";
    el.innerHTML =
      '<div class="gh-card" role="dialog" aria-labelledby="ghSetupTitle">' +
      '<h2 id="ghSetupTitle">Connect this device to your Deal Scout data</h2>' +
      '<div>Your searches and listings live in the private repo <code>' + repo() + '</code>. This page needs a GitHub token to read them. You do this once per device.</div>' +
      '<ol><li>Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">GitHub: new fine-grained token</a>.</li>' +
      '<li>Name it "Deal Scout page", expiration up to 1 year.</li>' +
      '<li>Repository access: <b>Only select repositories</b> and pick <code>dealscout-data</code>.</li>' +
      '<li>Permissions, Repository: <b>Contents: Read and write</b>. Leave everything else as is.</li>' +
      '<li>Generate, copy it, and paste it here.</li></ol>' +
      '<input id="ghToken" type="password" autocomplete="off" spellcheck="false" placeholder="github_pat_..." aria-label="GitHub token">' +
      '<button id="ghSave" type="button">Connect</button>' +
      '<div class="gh-err" role="status"></div>' +
      '<div class="gh-fine">The token stays in this browser on this device. It can only read and write dealscout-data.</div>' +
      "</div>";
    document.body.appendChild(el);
    if (message) el.querySelector(".gh-err").textContent = message;
    var input = el.querySelector("#ghToken"), btn = el.querySelector("#ghSave"), err = el.querySelector(".gh-err");
    function save() {
      var t = input.value.trim();
      if (!t) { err.textContent = "Paste the token first."; return; }
      btn.disabled = true; err.textContent = "Checking…";
      lsSet(TOKEN_KEY, t);
      gh("GET", "/repos/" + repo()).then(function (r) {
        if (r.status === 401) throw new Error("GitHub didn't accept that token. Copy it again.");
        if (r.status === 404 || r.status === 403) throw new Error("That token can't see " + repo() + ". Check the repository access step.");
        if (!r.ok) throw new Error("GitHub answered " + r.status + ". Try again in a minute.");
        return r.json();
      }).then(function () {
        el.hidden = true; err.textContent = ""; input.value = "";
        if (setupResolve) { var f = setupResolve; setupResolve = null; f(); }
        else refresh(true).catch(function () {});
      }).catch(function (e) {
        lsSet(TOKEN_KEY, null); err.textContent = e.message || String(e);
      }).then(function () { btn.disabled = false; });
    }
    btn.onclick = save;
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") save(); });
    setTimeout(function () { try { input.focus(); } catch (e) {} }, 50);
  }

  function ready() {
    if (document.readyState === "loading") return new Promise(function (res) { document.addEventListener("DOMContentLoaded", res, { once: true }); });
    return Promise.resolve();
  }

  function openDb() {
    return ready().then(function () {
      if (lsGet(TOKEN_KEY)) return;
      return new Promise(function (res) { setupResolve = res; showSetup(); });
    }).then(function () {
      return Promise.all([loadArch(), refresh(true).catch(function (e) { if (e.code !== "unauthenticated") throw e; return new Promise(function (res) { setupResolve = function () { refresh(true).then(res); }; }); })]);
    }).then(function () {
      startPolling(); notifyAll();
      return db;
    });
  }

  var dbPromise = null;
  window.claude = {
    use: function (name) {
      if (name === "db") { if (!dbPromise) dbPromise = openDb().catch(function (e) { console.error(e); dbPromise = null; throw e; }); return dbPromise; }
      if (name === "user") return Promise.resolve({ can: function () { return true; } });
      return Promise.resolve(null);
    }
  };
})();
