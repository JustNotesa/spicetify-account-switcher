// Headless-Test der Spicetify-Extension gegen einen Spicetify-Stub + Fake-Hilfsdienst.
// Aufruf: PLAYWRIGHT_PATH=/pfad/zu/node_modules/playwright node extension/test/run-tests.js
"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");
const { chromium } = require(process.env.PLAYWRIGHT_PATH || "playwright");
const { createFakeHelper } = require("./fakeHelper");

const EXT_DIR = path.resolve(__dirname, "..");
const results = [];
function check(name, cond, extra) {
  results.push({ name, ok: !!cond });
  console.log((cond ? "  ok   " : "  FAIL ") + name + (extra && !cond ? "  → " + JSON.stringify(extra) : ""));
}
const listen = (srv) => new Promise((r) => srv.listen(0, "127.0.0.1", () => r(srv.address().port)));

async function main() {
  // statischer Server für harness.html + accountSwitcher.js
  const staticSrv = http.createServer((req, res) => {
    const p = req.url.split("?")[0];
    const file = p === "/accountSwitcher.js" ? path.join(EXT_DIR, "accountSwitcher.js")
      : p === "/" || p === "/harness.html" ? path.join(__dirname, "harness.html") : null;
    if (!file) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "Content-Type": file.endsWith(".js") ? "text/javascript" : "text/html; charset=utf-8" });
    fs.createReadStream(file).pipe(res);
  });
  const sport = await listen(staticSrv);
  const ORIGIN = "http://127.0.0.1:" + sport;

  const fake = createFakeHelper({ allowedOrigins: ["https://xpui.app.spotify.com", ORIGIN] });
  const hport = await listen(fake.server);
  const HELPER = "http://127.0.0.1:" + hport;

  // freier, geschlossener Port für "Hilfsdienst läuft nicht"
  const tmp = http.createServer();
  const deadPort = await listen(tmp);
  await new Promise((r) => tmp.close(r));
  const DEAD = "http://127.0.0.1:" + deadPort;

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1100, height: 640 }, deviceScaleFactor: 2 });
  // Spotify-CDN-Bilder lokal beantworten (keine Netzanfragen nach außen)
  await ctx.route("https://i.scdn.co/**", (route) => route.fulfill({
    contentType: "image/svg+xml",
    body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#8d67ab"/>' +
      '<text x="32" y="42" font-size="28" font-family="Arial" font-weight="700" fill="#fff" text-anchor="middle">M</text></svg>',
  }));
  await ctx.route((url) => !/^(http:\/\/127\.0\.0\.1|https:\/\/i\.scdn\.co|data:)/.test(url.href), (route) => route.abort());
  const pageErrors = [];
  const newPage = async (query) => {
    const page = await ctx.newPage();
    page.on("pageerror", (e) => pageErrors.push(String(e)));
    page.on("console", (m) => { if (m.type() === "error") pageErrors.push(m.text()); });
    await page.goto(ORIGIN + "/harness.html?" + query);
    return page;
  };
  const posts = (p, f = fake) => f.requests.filter((r) => r.method === "POST" && r.path === p);
  const btnSel = ".main-actionButtons .msw-toggle";
  const row = (profile) => `[data-profile="${profile}"]`;
  const P_MAX = "default", P_ERIKA = "a20261101090000", P_FAM = "a20261102100000", P_ALT = "a20261103110000", P_NEW = "a20261104120000";
  const LOGIN_HINT = "Neues Spotify-Fenster öffnet sich – melde dich dort an (Passwort-Anmeldung im Browser).";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  try {
    // ---------------- 1. Normalbetrieb ----------------
    console.log("\n[1] Button, Registrierung, Menü");
    const t0 = Date.now();
    let page = await newPage("helper=" + encodeURIComponent(HELPER));
    await page.waitForSelector(btnSel, { timeout: 5000 });
    const pos = await page.evaluate(() => {
      const b = document.querySelector(".msw-toggle");
      const wrap = b.closest(".spicetify-topbar-button");
      return {
        firstInActions: wrap && wrap.parentElement.classList.contains("main-actionButtons") && wrap.parentElement.firstElementChild === wrap,
        haspopup: b.getAttribute("aria-haspopup"), expanded: b.getAttribute("aria-expanded"),
        hasSpotifyClass: b.classList.contains("main-topBar-buddyFeed"),
      };
    });
    check("Button sitzt als erstes in .main-actionButtons (links neben Profilbild)", pos.firstInActions, pos);
    check("Button hat Spotify-Klasse + aria-haspopup=menu", pos.hasSpotifyClass && pos.haspopup === "menu", pos);
    await page.waitForFunction(() => window.__mswAccountSwitcher && window.__mswAccountSwitcher.profile, null, { timeout: 3000 }).catch(() => {});
    const reg = posts("/register")[0];
    check("Start: POST /register mit username/displayName/imageUrl", reg && reg.body.username === "maxmuster" &&
      reg.body.displayName === "Max Mustermann" && reg.body.imageUrl === "https://i.scdn.co/image/mittel" &&
      reg.contentType === "application/json" && reg.origin === ORIGIN, reg);
    check("Start: /register kommt früh (< 1,5 s nach Seitenaufruf, Spicetify erscheint nach 0,3 s)", reg && reg.at - t0 < 1500, reg && { ms: reg.at - t0 });
    check("Antwort von /register → Profil-ID gemerkt", (await page.evaluate(() => window.__mswAccountSwitcher.profile)) === P_MAX);

    const regBefore = posts("/register").length;
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    check("Menü öffnen → erneut /register", posts("/register").length === regBefore + 1);
    const info = await page.evaluate(() => {
      const m = document.querySelector(".msw-menu");
      const items = [...m.querySelectorAll('[role^="menuitem"]')];
      return {
        names: items.map((i) => i.querySelector(".msw-name").textContent),
        checked: items.filter((i) => i.getAttribute("aria-checked") === "true").map((i) => i.dataset.profile),
        checkIcons: m.querySelectorAll(".msw-current .msw-check svg").length,
        lastIsAdd: items[items.length - 1].classList.contains("msw-add") && items[items.length - 1].textContent.includes("Konto hinzufügen"),
        dividerBeforeAdd: items[items.length - 1].previousElementSibling?.classList.contains("msw-divider"),
        initials: [...m.querySelectorAll(".msw-avatar")].map((a) => a.querySelector("img") ? "img" : a.textContent),
        removeButtons: [...m.querySelectorAll(".msw-remove")].map((b) => b.closest("li").querySelector("[data-profile]").dataset.profile),
        subs: Object.fromEntries([...m.querySelectorAll("[data-profile]")].map((b) => [b.dataset.profile, b.querySelector(".msw-sub").textContent])),
        labels: Object.fromEntries([...m.querySelectorAll("[data-profile]")].map((b) => [b.dataset.profile, b.getAttribute("aria-label")])),
        expanded: document.querySelector(".msw-toggle").getAttribute("aria-expanded"),
        focused: document.activeElement?.dataset?.profile,
        rect: m.getBoundingClientRect().toJSON(), btn: document.querySelector(".msw-toggle").getBoundingClientRect().toJSON(),
      };
    });
    check("Liste: aktuelles Konto zuerst, alle Konten", JSON.stringify(info.names) ===
      JSON.stringify(["Max Mustermann", "Altes Konto", "Erika Musterfrau", "Familie", "Neues Profil", "Konto hinzufügen"]), info.names);
    check("Nur aktuelles Konto markiert (aria-checked + Haken)", JSON.stringify(info.checked) === JSON.stringify([P_MAX]) && info.checkIcons === 1, info);
    check("„Konto hinzufügen“ ist letztes Element, davor Trenner", info.lastIsAdd && info.dividerBeforeAdd, info);
    check("Initiale bei fehlendem Bild ('F')", info.initials.includes("F"), info.initials);
    await page.waitForFunction(() => [...document.querySelectorAll(".msw-avatar")].filter((a) => !a.querySelector("img")).length >= 2, null, { timeout: 3000 }).catch(() => {});
    const initials2 = await page.evaluate(() => [...document.querySelectorAll(".msw-avatar")].map((a) => (a.querySelector("img") ? "img" : a.textContent)));
    check("Kaputtes Bild → Initiale ('A')", initials2.includes("A"), initials2);
    check("Entfernen-× nur bei nicht-aktuellen Profilen (nie beim Standardprofil)", !info.removeButtons.includes(P_MAX) && info.removeButtons.length === 4, info.removeButtons);
    check("„Nicht angemeldet“ bei loggedIn:false", info.subs[P_NEW] === "Nicht angemeldet" &&
      Object.values(info.subs).filter((t) => t.includes("Nicht angemeldet")).length === 1, info.subs);
    check("Kein „Anmeldung fehlt“ mehr", !Object.values(info.subs).some((t) => t.includes("Anmeldung fehlt")), info.subs);
    check("Sub-Zeilen: Nutzername, aktuelles mit „· aktiv“", info.subs[P_MAX] === "maxmuster · aktiv" && info.subs[P_ERIKA] === "erika_m", info.subs);
    check("aria-label nennt „nicht angemeldet“ bzw. „aktuelles Konto“", /nicht angemeldet/.test(info.labels[P_NEW]) &&
      /aktuelles Konto/.test(info.labels[P_MAX]), info.labels);
    check("aria-expanded=true, Fokus im Menü", info.expanded === "true" && info.focused === P_MAX, info);
    check("Menü unter dem Button, rechtsbündig im Fenster", info.rect.top >= info.btn.bottom && info.rect.right <= 1100, info);

    // Screenshot mit Hover auf einer Zeile (zeigt ×)
    await page.hover(row(P_ERIKA));
    await new Promise((r) => setTimeout(r, 200));
    const shot = path.join(__dirname, "menu.png");
    await page.screenshot({ path: shot, clip: { x: 500, y: 0, width: 600, height: 520 } });
    console.log("  Screenshot: " + shot);

    // Tastatur
    await page.focus(row(P_MAX));
    await page.keyboard.press("ArrowDown");
    const f1 = await page.evaluate(() => document.activeElement.dataset.profile || document.activeElement.className);
    await page.keyboard.press("End");
    const f2 = await page.evaluate(() => document.activeElement.textContent.trim());
    check("Pfeiltasten/End navigieren im Menü", f1 === P_ALT && f2 === "Konto hinzufügen", { f1, f2 });
    await page.keyboard.press("Escape");
    const esc = await page.evaluate(() => ({ hidden: document.querySelector(".msw-menu").hidden, focusBtn: document.activeElement.classList.contains("msw-toggle") }));
    check("Escape schließt, Fokus zurück auf Button", esc.hidden && esc.focusBtn, esc);

    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    await page.mouse.click(300, 400);
    check("Klick außerhalb schließt", await page.evaluate(() => document.querySelector(".msw-menu").hidden));
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    await page.click(btnSel);
    check("Erneuter Klick auf Button schließt (Toggle)", await page.evaluate(() => document.querySelector(".msw-menu").hidden));

    // Wechseln
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    await page.click(row(P_ERIKA));
    await page.waitForFunction(() => window.__notifications.length > 0);
    const sw = posts("/switch").at(-1);
    let notes = await page.evaluate(() => window.__notifications);
    check("Klick auf anderes Konto → POST /switch {profile}", sw && JSON.stringify(sw.body) === JSON.stringify({ profile: P_ERIKA }) &&
      sw.contentType === "application/json", sw);
    check("202 → Benachrichtigung „Wechsle zu „…“ …“ (ohne „neu gestartet“)", notes.at(-1).text === "Wechsle zu „Erika Musterfrau“ …" &&
      !notes.at(-1).isError, notes);
    check("Menü nach Wechsel geschlossen", await page.evaluate(() => document.querySelector(".msw-menu").hidden));

    // Nicht angemeldetes Profil ist anklickbar → Wechsel + Anmelde-Hinweis
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    await page.click(row(P_NEW));
    await page.waitForFunction(() => window.__notifications.length > 1);
    notes = await page.evaluate(() => window.__notifications);
    check("„Nicht angemeldet“ anklickbar → POST /switch {profile}", posts("/switch").at(-1).body.profile === P_NEW);
    check("…Benachrichtigung mit Anmelde-Hinweis", notes.at(-1).text.startsWith("Wechsle zu „Neues Profil“ …") &&
      notes.at(-1).text.includes(LOGIN_HINT) && !notes.at(-1).isError, notes.at(-1));
    check("…Menü geschlossen", await page.evaluate(() => document.querySelector(".msw-menu").hidden));

    // Profil inzwischen weg → 404
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    const altBackup = fake.state.profiles[P_ALT];
    delete fake.state.profiles[P_ALT];
    await page.click(row(P_ALT));
    await page.waitForFunction(() => window.__notifications.length > 2);
    fake.state.profiles[P_ALT] = altBackup;
    notes = await page.evaluate(() => window.__notifications);
    check("404 bei /switch → Fehlermeldung", notes.at(-1).isError && notes.at(-1).text.includes("nicht gefunden"), notes.at(-1));

    // Aktuelles Konto anklicken → kein /switch
    const nSwitch = posts("/switch").length;
    await page.click(row(P_MAX));
    check("Klick auf aktuelles Konto → kein /switch, Menü zu", posts("/switch").length === nSwitch &&
      await page.evaluate(() => document.querySelector(".msw-menu").hidden));

    // Entfernen
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    await page.hover(row(P_FAM));
    await page.click(`li:has(${row(P_FAM)}) .msw-remove`);
    await page.waitForSelector(".msw-confirm");
    check("× zeigt Rückfrage im Menü, noch kein /remove", posts("/remove").length === 0);
    await page.click('[data-msw-action="cancel-remove"]');
    check("Abbrechen stellt Zeile wieder her", await page.$(row(P_FAM)) !== null && posts("/remove").length === 0);
    await page.click(`li:has(${row(P_FAM)}) .msw-remove`);
    await page.click('[data-msw-action="confirm-remove"]');
    await page.waitForFunction((sel) => !document.querySelector(sel), row(P_FAM));
    const rm = posts("/remove").at(-1);
    check("Bestätigen → POST /remove {profile}, Zeile weg", rm && JSON.stringify(rm.body) === JSON.stringify({ profile: P_FAM }), rm);
    check("Menü bleibt nach Entfernen offen", !(await page.evaluate(() => document.querySelector(".msw-menu").hidden)));

    // Konto hinzufügen
    await page.click(".msw-add");
    await page.waitForFunction((t) => window.__notifications.some((n) => n.text === t), LOGIN_HINT);
    const add = posts("/add").at(-1);
    notes = await page.evaluate(() => window.__notifications);
    check("„Konto hinzufügen“ → POST /add {}", add && JSON.stringify(add.body) === "{}", add);
    check("202 → Benachrichtigung „Neues Spotify-Fenster öffnet sich – melde dich dort an …“", notes.at(-1).text === LOGIN_HINT && !notes.at(-1).isError, notes.at(-1));
    check("Kein Text „neu gestartet“ mehr", !notes.some((n) => n.text.includes("neu gestartet")), notes);

    // Doppelte Injektion
    await page.addScriptTag({ url: "/accountSwitcher.js" });
    await new Promise((r) => setTimeout(r, 800));
    check("Doppelte Injektion → nur ein Button, ein Style", await page.evaluate(() =>
      document.querySelectorAll(".msw-toggle").length === 1 && document.querySelectorAll("#msw-style").length === 1));
    await page.close();

    // ---------------- 2. Hilfsdienst nicht erreichbar ----------------
    console.log("\n[2] Hilfsdienst nicht erreichbar");
    page = await newPage("helper=" + encodeURIComponent(DEAD));
    await page.waitForSelector(btnSel);
    await page.click(btnSel);
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-hint");
    const un = await page.evaluate(() => {
      const m = document.querySelector(".msw-menu");
      const add = m.querySelector(".msw-add");
      return { hint: m.querySelector(".msw-hint").textContent, list: !!m.querySelector(".msw-list"),
               addDisabled: add.getAttribute("aria-disabled"), addLast: m.lastElementChild === add };
    });
    check("Hinweis „Hilfsdienst läuft nicht – install.sh ausführen“", un.hint.includes("Hilfsdienst läuft nicht") && un.hint.includes("install.sh"), un);
    check("Keine Kontoliste, „Konto hinzufügen“ deaktiviert + letzter Eintrag", !un.list && un.addDisabled === "true" && un.addLast, un);
    const before = await page.evaluate(() => window.__notifications.length);
    await page.click(".msw-add", { force: true });
    await new Promise((r) => setTimeout(r, 300));
    check("Klick auf deaktiviertes „Konto hinzufügen“ tut nichts", (await page.evaluate(() => window.__notifications.length)) === before &&
      !(await page.evaluate(() => document.querySelector(".msw-menu").hidden)));
    await page.screenshot({ path: path.join(__dirname, "menu-offline.png"), clip: { x: 500, y: 0, width: 600, height: 260 } });
    await page.close();

    // ---------------- 3. CosmosAsync-Fallback für Nutzer ----------------
    console.log("\n[3] Nutzer über CosmosAsync /v1/me");
    const nReg = posts("/register").length;
    page = await newPage("user=cosmos&helper=" + encodeURIComponent(HELPER));
    await page.waitForSelector(btnSel);
    await page.waitForFunction(() => (window.__cosmosCalls || []).length > 0);
    await new Promise((r) => setTimeout(r, 400));
    const reg3 = posts("/register").slice(nReg)[0];
    check("Ohne UserAPI → /v1/me → /register", reg3 && reg3.body.username === "maxmuster" && reg3.body.imageUrl === "https://i.scdn.co/image/mittel", reg3);
    await page.close();

    // ---------------- 4. DOM-Fallback ----------------
    console.log("\n[4] DOM-Fallback (Topbar-Button wird nicht eingehängt)");
    const nReg4 = posts("/register").length;
    const t4 = Date.now();
    page = await newPage("topbar=broken&helper=" + encodeURIComponent(HELPER));
    await sleep(1500);
    const reg4 = posts("/register").slice(nReg4)[0];
    check("/register kommt sofort, nicht erst nach Topbar-/Fallback-Wartezeit", reg4 && reg4.at - t4 < 1500, reg4 && { ms: reg4.at - t4 });
    await page.waitForSelector(".main-actionButtons > .msw-fallback-btn", { timeout: 15000 });
    check("Fallback-Button in .main-actionButtons eingefügt", true);
    await page.evaluate(() => document.querySelector(".msw-fallback-btn").remove());
    await page.waitForSelector(".main-actionButtons > .msw-fallback-btn", { timeout: 3000 }).then(() => true, () => false)
      .then((ok) => check("MutationObserver setzt entfernten Button wieder ein", ok));
    // Leiste neu rendern (wie bei React-Re-Render)
    await page.evaluate(() => {
      const a = document.querySelector(".main-actionButtons");
      const n = a.cloneNode(false);
      n.innerHTML = '<button class="main-topBar-buddyFeed">x</button>';
      a.replaceWith(n);
    });
    await page.waitForSelector(".main-actionButtons > .msw-fallback-btn", { timeout: 3000 }).then(() => true, () => false)
      .then((ok) => check("…auch nach Neu-Rendern der Leiste", ok));
    await page.click(".msw-fallback-btn");
    await page.waitForSelector(".msw-menu:not([hidden]) .msw-list");
    check("Fallback-Button öffnet Menü", await page.evaluate(() => document.querySelector(".msw-fallback-btn").getAttribute("aria-expanded") === "true"));
    await page.close();

    // ---------------- 5. Hilfsdienst startet verzögert (Socket-Aktivierung) ----------------
    console.log("\n[5] Hilfsdienst erst nach ~2,5 s erreichbar → Registrierung wird wiederholt");
    const tmp2 = http.createServer();
    const latePort = await listen(tmp2);
    await new Promise((r) => tmp2.close(r));
    const late = createFakeHelper({ allowedOrigins: [ORIGIN] });
    page = await newPage("helper=" + encodeURIComponent("http://127.0.0.1:" + latePort));
    await sleep(2500);
    await new Promise((r) => late.server.listen(latePort, "127.0.0.1", r));
    const lateOk = await page.waitForFunction(() => window.__mswAccountSwitcher && window.__mswAccountSwitcher.profile, null, { timeout: 4000 })
      .then(() => true, () => false);
    check("Nach Fehlversuchen: /register beim spät gestarteten Dienst angekommen", lateOk && posts("/register", late).length === 1,
      { lateOk, n: posts("/register", late).length });
    await page.close();
    late.server.close();

    console.log("\n[6] Langsamer erster Start (4 s bis zur Antwort) → kein Abbruch nach 3 s");
    fake.delays["/register"] = 4000;
    const nReg6 = posts("/register").length;
    page = await newPage("helper=" + encodeURIComponent(HELPER));
    const slowOk = await page.waitForFunction(() => window.__mswAccountSwitcher && window.__mswAccountSwitcher.profile, null, { timeout: 7000 })
      .then(() => true, () => false);
    delete fake.delays["/register"];
    check("Erste Anfrage wartet bis 5 s (Antwort nach 4 s angenommen, kein zweiter Versuch)", slowOk && posts("/register").length - nReg6 === 1,
      { slowOk, n: posts("/register").length - nReg6 });
    await page.close();

    check("Keine JS-Fehler auf der Seite", pageErrors.filter((e) => !/kaputt\.png|ERR_CONNECTION_REFUSED|Failed to load resource/.test(e)).length === 0, pageErrors);
  } finally {
    await browser.close();
    fake.server.close();
    staticSrv.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} Prüfungen bestanden`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
