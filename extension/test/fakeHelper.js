// Fake-Hilfsdienst nach docs/ARCHITECTURE.md v2 (nur für Tests, keine Dateien, kein Spotify-Start/-Beenden).
// Nur erfundene Beispielnamen verwenden (Max Mustermann, Erika Musterfrau …).
// Standalone: node fakeHelper.js [port]  (Default 48656, damit der echte Dienst auf 48655 nicht stört)
"use strict";
const http = require("http");

const svgAvatar = (bg, letter) =>
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="${bg}"/>` +
      `<text x="32" y="42" font-size="28" font-family="Arial" font-weight="700" fill="#fff" text-anchor="middle">${letter}</text></svg>`
  );

const LOGIN_URL =
  "https://accounts.spotify.com/logout?continue=" +
  encodeURIComponent(
    "https://accounts.spotify.com/de/login?allow_password=1&continue=" +
      encodeURIComponent("https://accounts.spotify.com/de/status")
  );

function initialState() {
  return {
    current: "default",
    seq: 0,
    profiles: {
      default: { username: "maxmuster", displayName: "Max Mustermann", imageUrl: svgAvatar("#8d67ab", "M"), loggedIn: true },
      a20261101090000: { username: "erika_m", displayName: "Erika Musterfrau", imageUrl: svgAvatar("#e8115b", "E"), loggedIn: true },
      a20261102100000: { username: "familie123", displayName: "Familie", imageUrl: null, loggedIn: true },
      a20261103110000: { username: "alt_konto", displayName: "Altes Konto", imageUrl: "http://127.0.0.1:1/kaputt.png", loggedIn: true },
      // frisch über „Konto hinzufügen“ angelegt, Anmeldung nie abgeschlossen
      a20261104120000: { username: null, displayName: null, imageUrl: null, loggedIn: false },
    },
  };
}

// delays: {"/pfad": ms} → Antwort auf diesen Pfad verzögern (simuliert Socket-Aktivierung/Python-Start)
function createFakeHelper({ allowedOrigins = ["https://xpui.app.spotify.com"] } = {}) {
  const state = initialState();
  const requests = [];
  const delays = {};

  function listAccounts() {
    const arr = Object.entries(state.profiles).map(([profile, p]) => ({
      profile,
      username: p.username,
      displayName: p.displayName,
      imageUrl: p.imageUrl,
      running: profile === state.current,
      current: profile === state.current,
      loggedIn: p.loggedIn,
    }));
    const key = (a) => a.displayName || a.username || "\uffff" + a.profile; // namenlose Profile ans Ende
    arr.sort((a, b) => b.current - a.current || key(a).localeCompare(key(b)));
    return { accounts: arr };
  }

  const server = http.createServer((req, res) => {
    const origin = req.headers.origin;
    const send = (status, obj) => {
      const h = { "Content-Type": "application/json", Vary: "Origin" };
      if (origin && allowedOrigins.includes(origin)) {
        h["Access-Control-Allow-Origin"] = origin;
        h["Access-Control-Allow-Methods"] = "GET, POST";
        h["Access-Control-Allow-Headers"] = "Content-Type";
        h["Access-Control-Allow-Private-Network"] = "true";
      }
      res.writeHead(status, h);
      res.end(obj === undefined ? "" : JSON.stringify(obj));
    };
    if (origin && !allowedOrigins.includes(origin)) return send(403, { ok: false, error: "origin" });

    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      let body = null;
      try {
        body = raw ? JSON.parse(raw) : null;
      } catch (_) {
        return send(400, { ok: false, error: "bad json" });
      }
      const path = req.url.split("?")[0];
      requests.push({ method: req.method, path, body, contentType: req.headers["content-type"] || null, origin, at: Date.now() });
      if (req.method === "OPTIONS") return send(204);
      const delay = req.method === "POST" || req.method === "GET" ? delays[path] || 0 : 0;
      setTimeout(() => handle(req.method, path, body, send), delay);
    });
  });

  function handle(method, path, body, send) {
    if (method === "GET" && path === "/health") return send(200, { ok: true, version: "2.0.0" });
    if (method === "GET" && path === "/accounts") return send(200, listAccounts());
    if (method === "POST" && path === "/register") {
      const { username, displayName, imageUrl } = body || {};
      if (!username) return send(400, { ok: false, error: "username fehlt" });
      const hits = Object.keys(state.profiles).filter((id) => state.profiles[id].username === username);
      if (!hits.length) return send(404, { ok: false, error: "kein Profil für dieses Konto" });
      const id = hits.includes(state.current) ? state.current : hits[0];
      Object.assign(state.profiles[id], { displayName: displayName || username, imageUrl: imageUrl || null });
      return send(200, { ok: true, profile: id });
    }
    if (method === "POST" && path === "/switch") {
      const id = body && body.profile;
      if (!id || !state.profiles[id]) return send(404, { ok: false, error: "unbekanntes Profil" });
      if (id === state.current) return send(200, { ok: true, switching: false });
      return send(202, { ok: true, switching: true });
    }
    if (method === "POST" && path === "/add") {
      const id = "a2026111112000" + state.seq++;
      state.profiles[id] = { username: null, displayName: null, imageUrl: null, loggedIn: false };
      return send(202, { ok: true, profile: id, loginUrl: LOGIN_URL });
    }
    if (method === "POST" && path === "/remove") {
      const id = body && body.profile;
      if (id === "default" || id === state.current) return send(409, { ok: false, error: "Profil läuft oder ist Standard" });
      if (!id || !state.profiles[id]) return send(404, { ok: false, error: "unbekanntes Profil" });
      delete state.profiles[id];
      return send(200, { ok: true });
    }
    send(404, { ok: false, error: "not found" });
  }

  return {
    server,
    state,
    requests,
    delays,
    reset: () => {
      Object.assign(state, initialState());
      for (const k of Object.keys(delays)) delete delays[k];
    },
  };
}

module.exports = { createFakeHelper };

if (require.main === module) {
  const port = Number(process.argv[2] || 48656);
  const { server } = createFakeHelper({ allowedOrigins: ["https://xpui.app.spotify.com", "http://127.0.0.1:8080"] });
  server.listen(port, "127.0.0.1", () => console.log("Fake-Hilfsdienst auf http://127.0.0.1:" + port));
}
