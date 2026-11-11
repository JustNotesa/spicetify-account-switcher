// NAME: Account Switcher
// AUTHOR: spotify-multiaccount
// DESCRIPTION: Kontowechsel-Menü in der oberen Leiste. Der eigentliche Wechsel läuft über den
//              lokalen Hilfsdienst auf http://127.0.0.1:48655 (Schnittstelle v2, siehe docs/ARCHITECTURE.md):
//              ein Spotify-Profil je Konto, Wechsel = neues Fenster öffnet sich, altes schließt sich.
// VERSION: 2.0.0

(function mswAccountSwitcher() {
  "use strict";

  // ---- Doppelte Injektion verhindern -------------------------------------------------------
  if (window.__mswAccountSwitcherLoaded) return;
  window.__mswAccountSwitcherLoaded = true;

  // Nur für Tests überschreibbar (Harness setzt window.__MSW_HELPER_URL__ vor dem Laden).
  const HELPER = String(window.__MSW_HELPER_URL__ || "http://127.0.0.1:48655").replace(/\/+$/, "");
  // Der Hilfsdienst ist socket-aktiviert: Nach ~60 s Leerlauf beendet er sich, die nächste Anfrage startet
  // Python neu. Darum großzügiges Timeout, solange seit einer Weile keine Antwort kam, danach kürzer.
  const TIMEOUT_COLD_MS = 5000;
  const TIMEOUT_WARM_MS = 3000;
  const WARM_WINDOW_MS = 45000;
  const REGISTER_RETRIES = 5;
  const REGISTER_RETRY_DELAY_MS = 1000;
  const P = "msw-"; // CSS-Klassen-Präfix

  const ICON_PEOPLE =
    '<svg role="img" height="16" width="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
    'stroke-width="1.4" stroke-linecap="round" aria-hidden="true">' +
    '<circle cx="6" cy="5" r="2.6"/>' +
    '<path d="M1.2 14.2c0-2.8 2.1-4.6 4.8-4.6s4.8 1.8 4.8 4.6"/>' +
    '<path d="M10.4 2.5a2.6 2.6 0 0 1 0 5"/>' +
    '<path d="M12.2 9.9c1.6.6 2.6 2.1 2.6 4.3"/></svg>';
  const ICON_PLUS =
    '<svg height="16" width="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">' +
    '<path d="M15.25 8a.75.75 0 0 1-.75.75H8.75v5.75a.75.75 0 0 1-1.5 0V8.75H1.5a.75.75 0 0 1 0-1.5h5.75V1.5a.75.75 0 0 1 1.5 0v5.75h5.75a.75.75 0 0 1 .75.75z"/></svg>';
  const ICON_CHECK =
    '<svg height="16" width="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">' +
    '<path d="M15.53 2.47a.75.75 0 0 1 0 1.06L4.82 14.24.47 9.9a.75.75 0 1 1 1.06-1.06l3.29 3.29 9.65-9.66a.75.75 0 0 1 1.06 0z"/></svg>';
  const ICON_X =
    '<svg height="12" width="12" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">' +
    '<path d="M2.47 2.47a.75.75 0 0 1 1.06 0L8 6.94l4.47-4.47a.75.75 0 1 1 1.06 1.06L9.06 8l4.47 4.47a.75.75 0 1 1-1.06 1.06L8 9.06l-4.47 4.47a.75.75 0 0 1-1.06-1.06L6.94 8 2.47 3.53a.75.75 0 0 1 0-1.06z"/></svg>';

  // ---- Hilfsfunktionen ---------------------------------------------------------------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(fn, { interval = 300, timeout = 0 } = {}) {
    const start = Date.now();
    for (;;) {
      try {
        const v = fn();
        if (v) return v;
      } catch (_) {
        /* weiter warten */
      }
      if (timeout && Date.now() - start > timeout) return null;
      await sleep(interval);
    }
  }

  function el(tag, cls, attrs) {
    const n = document.createElement(tag);
    if (cls) n.className = cls.split(" ").map((c) => P + c).join(" ");
    if (attrs) for (const k of Object.keys(attrs)) n.setAttribute(k, attrs[k]);
    return n;
  }

  function notify(text, isError) {
    try {
      if (window.Spicetify && typeof Spicetify.showNotification === "function") {
        Spicetify.showNotification(text, !!isError, isError ? 6000 : 4000);
        return;
      }
    } catch (_) {
      /* fällt auf console zurück */
    }
    console[isError ? "warn" : "log"]("[AccountSwitcher]", text);
  }

  // ---- Hilfsdienst-Anfragen ----------------------------------------------------------------
  // Liefert {ok, status, data}; Netzwerkfehler/Timeout → {ok:false, status:0, unreachable:true}
  let lastContact = 0; // Zeitpunkt der letzten Antwort des Hilfsdienstes

  async function helper(method, path, body) {
    const ctrl = new AbortController();
    const warm = lastContact && Date.now() - lastContact < WARM_WINDOW_MS;
    const timer = setTimeout(() => ctrl.abort(), warm ? TIMEOUT_WARM_MS : TIMEOUT_COLD_MS);
    try {
      const init = { method, signal: ctrl.signal, cache: "no-store" };
      if (method !== "GET") {
        init.headers = { "Content-Type": "application/json" };
        init.body = JSON.stringify(body || {});
      }
      const res = await fetch(HELPER + path, init);
      lastContact = Date.now();
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        data = null;
      }
      return { ok: res.ok && !(data && data.ok === false), status: res.status, data };
    } catch (e) {
      return { ok: false, status: 0, unreachable: true, error: String(e && e.message || e) };
    } finally {
      clearTimeout(timer);
    }
  }

  // ---- Aktuelles Spotify-Konto -------------------------------------------------------------
  let me = null; // {username, displayName, imageUrl}
  let myProfile = null; // Profil-ID laut /register

  function pickImage(images) {
    if (!Array.isArray(images) || !images.length) return null;
    // größtes Bild bevorzugen, aber nicht riesig
    const sorted = images
      .filter((i) => i && i.url)
      .sort((a, b) => (a.width || a.maxWidth || 0) - (b.width || b.maxWidth || 0));
    const mid = sorted.find((i) => (i.width || i.maxWidth || 0) >= 64);
    return (mid || sorted[sorted.length - 1] || {}).url || null;
  }

  async function loadMe() {
    // 1) Spicetify.Platform.UserAPI.getUser() → {username, displayName, images:[{url,width,height}]}
    try {
      const api = Spicetify.Platform && Spicetify.Platform.UserAPI;
      if (api && typeof api.getUser === "function") {
        const u = await api.getUser();
        if (u && u.username) {
          me = {
            username: String(u.username),
            displayName: String(u.displayName || u.display_name || u.name || u.username),
            imageUrl: pickImage(u.images) || u.avatarUrl || u.imageUrl || u.image_url || null,
          };
          return me;
        }
      }
    } catch (e) {
      console.warn("[AccountSwitcher] UserAPI.getUser fehlgeschlagen", e);
    }
    // 2) Web-API über CosmosAsync
    try {
      if (Spicetify.CosmosAsync && typeof Spicetify.CosmosAsync.get === "function") {
        const u = await Spicetify.CosmosAsync.get("https://api.spotify.com/v1/me");
        if (u && u.id) {
          me = {
            username: String(u.id),
            displayName: String(u.display_name || u.id),
            imageUrl: pickImage(u.images),
          };
          return me;
        }
      }
    } catch (e) {
      console.warn("[AccountSwitcher] /v1/me fehlgeschlagen", e);
    }
    return me;
  }

  // /register = Metadaten melden UND „UI bereit“-Signal für einen laufenden Wechsel (docs/ARCHITECTURE.md).
  async function register() {
    if (!me) await loadMe();
    if (!me) return { ok: false };
    const res = await helper("POST", "/register", {
      username: me.username,
      displayName: me.displayName,
      imageUrl: me.imageUrl || null,
    });
    if (res.ok && res.data && res.data.profile) myProfile = String(res.data.profile);
    return res;
  }

  // Beim Start: so früh wie möglich melden. Der Hilfsdienst wird evtl. gerade erst per launchd gestartet →
  // bei Nichterreichbarkeit einige Male wiederholen.
  async function registerOnStartup() {
    await waitFor(() => (Spicetify.Platform && Spicetify.Platform.UserAPI) || Spicetify.CosmosAsync, {
      interval: 100,
      timeout: 15000,
    });
    let r = { ok: false };
    for (let i = 0; i < REGISTER_RETRIES; i++) {
      r = await register();
      if (!r.unreachable) break;
      if (i < REGISTER_RETRIES - 1) await sleep(REGISTER_RETRY_DELAY_MS);
    }
    if (!r.ok) console.info("[AccountSwitcher] Registrierung nicht möglich", r.status || r.error || "");
    return r;
  }

  // ---- Styles ------------------------------------------------------------------------------
  function injectStyle() {
    if (document.getElementById("msw-style")) return;
    const s = document.createElement("style");
    s.id = "msw-style";
    s.textContent = `
[class^="msw-"][hidden],[class*=" msw-"][hidden]{display:none!important}
.msw-menu{position:fixed;z-index:10000;min-width:260px;max-width:340px;max-height:calc(100vh - 80px);
  overflow-y:auto;box-sizing:border-box;padding:4px;border-radius:4px;
  background:var(--spice-main-elevated,var(--background-elevated-highlight,#282828));
  color:var(--spice-text,var(--text-base,#fff));
  box-shadow:0 16px 24px rgba(0,0,0,.3),0 6px 8px rgba(0,0,0,.2);
  font-family:var(--encore-body-font-stack,inherit);font-size:.875rem;line-height:1.25;
  animation:msw-in .12s ease-out}
@keyframes msw-in{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.msw-header{padding:8px 12px 6px;font-size:.75rem;font-weight:700;letter-spacing:.02em;
  color:var(--spice-subtext,var(--text-subdued,#b3b3b3))}
.msw-list{list-style:none;margin:0;padding:0}
.msw-row{position:relative;display:flex;align-items:center}
.msw-item{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:12px;width:100%;
  min-height:48px;padding:6px 8px 6px 8px;border-radius:2px;cursor:pointer;
  color:var(--spice-text,#fff)}
.msw-row.msw-removable .msw-item{padding-right:40px}
.msw-item:hover,.msw-item:focus-visible{background:rgba(var(--spice-rgb-selected-row,255,255,255),.1)}
.msw-item:focus-visible{outline:2px solid var(--spice-text,#fff);outline-offset:-2px}
.msw-item[aria-disabled="true"]{cursor:default;opacity:.5}
.msw-item[aria-disabled="true"]:hover{background:transparent}
.msw-avatar{flex:0 0 32px;width:32px;height:32px;border-radius:50%;overflow:hidden;display:flex;
  align-items:center;justify-content:center;font-weight:700;font-size:.875rem;
  background:var(--spice-button-disabled,#535353);color:var(--spice-text,#fff)}
.msw-avatar img{width:100%;height:100%;object-fit:cover;display:block}
.msw-texts{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;gap:2px}
.msw-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:400}
.msw-current .msw-name{color:var(--spice-button,var(--text-bright-accent,#1ed760));font-weight:700}
.msw-sub{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.75rem;
  color:var(--spice-subtext,var(--text-subdued,#b3b3b3))}
.msw-sub.msw-nologin{font-style:italic}
.msw-check{flex:0 0 16px;display:flex;color:var(--spice-button,var(--text-bright-accent,#1ed760))}
.msw-remove{all:unset;box-sizing:border-box;position:absolute;right:8px;top:50%;margin-top:-12px;
  width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  cursor:pointer;color:var(--spice-subtext,#b3b3b3);opacity:0;transition:opacity .1s}
.msw-row:hover .msw-remove,.msw-remove:focus-visible{opacity:1}
.msw-remove:hover,.msw-remove:focus-visible{color:var(--spice-text,#fff);
  background:rgba(var(--spice-rgb-selected-row,255,255,255),.1)}
.msw-remove:focus-visible{outline:2px solid var(--spice-text,#fff)}
.msw-confirm{display:flex;align-items:center;gap:8px;width:100%;min-height:48px;padding:6px 8px;
  box-sizing:border-box}
.msw-confirm-text{flex:1 1 auto;min-width:0;font-size:.8125rem}
.msw-pill{all:unset;box-sizing:border-box;cursor:pointer;padding:4px 12px;border-radius:9999px;
  font-size:.75rem;font-weight:700;border:1px solid var(--spice-subtext,#7c7c7c);color:var(--spice-text,#fff)}
.msw-pill:hover{border-color:var(--spice-text,#fff)}
.msw-pill:focus-visible{outline:2px solid var(--spice-text,#fff);outline-offset:1px}
.msw-pill.msw-danger{background:var(--spice-notification-error,#e91429);border-color:transparent}
.msw-divider{height:1px;margin:4px 0;border:0;background:rgba(var(--spice-rgb-text,255,255,255),.1)}
.msw-add-icon{flex:0 0 32px;width:32px;height:32px;display:flex;align-items:center;justify-content:center;
  color:var(--spice-subtext,#b3b3b3)}
.msw-item:hover .msw-add-icon{color:var(--spice-text,#fff)}
.msw-hint{padding:10px 12px;font-size:.8125rem;color:var(--spice-subtext,#b3b3b3)}
.msw-hint strong{color:var(--spice-text,#fff);font-weight:700;display:block;margin-bottom:2px}
.msw-hint code{font-family:var(--encore-mono-font-stack,monospace);font-size:.75rem}
.msw-loading{padding:14px 12px;font-size:.8125rem;color:var(--spice-subtext,#b3b3b3)}
.msw-fallback-btn{all:unset;box-sizing:border-box;width:32px;height:32px;border-radius:50%;
  display:inline-flex;align-items:center;justify-content:center;cursor:pointer;
  color:var(--spice-subtext,#b3b3b3);margin-inline-end:8px}
.msw-fallback-btn:hover,.msw-fallback-btn[aria-expanded="true"]{color:var(--spice-text,#fff);transform:scale(1.04)}
.msw-fallback-btn:focus-visible{outline:2px solid var(--spice-text,#fff)}
`;
    (document.head || document.documentElement).appendChild(s);
  }

  // ---- Menü --------------------------------------------------------------------------------
  let anchorBtn = null; // aktuell sichtbarer Button (Topbar oder Fallback)
  let menu = null;
  let openToken = 0;

  function isOpen() {
    return !!(menu && !menu.hidden);
  }

  function ensureMenu() {
    if (menu) return menu;
    menu = el("div", "menu", { role: "menu", id: "msw-menu", "aria-label": "Konto wechseln" });
    menu.hidden = true;
    menu.addEventListener("keydown", onMenuKeydown);
    document.body.appendChild(menu);
    return menu;
  }

  function positionMenu() {
    if (!menu || !anchorBtn || !anchorBtn.isConnected) return;
    const r = anchorBtn.getBoundingClientRect();
    const w = menu.offsetWidth || 280;
    let left = r.right - w;
    left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
    menu.style.left = left + "px";
    menu.style.top = Math.round(r.bottom + 8) + "px";
  }

  function focusables() {
    return menu ? Array.from(menu.querySelectorAll("[data-msw-focus]:not([disabled])")) : [];
  }

  function onMenuKeydown(e) {
    const list = focusables();
    const idx = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      (list[(idx + 1) % list.length] || list[0])?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      (list[(idx - 1 + list.length) % list.length] || list[list.length - 1])?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      list[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      list[list.length - 1]?.focus();
    } else if (e.key === "Tab") {
      closeMenu(false);
    }
  }

  function onDocPointerDown(e) {
    if (!isOpen()) return;
    const t = e.target;
    if (menu.contains(t)) return;
    if (anchorBtn && anchorBtn.contains(t)) return; // Toggle übernimmt
    closeMenu(false);
  }

  function onDocKeydown(e) {
    if (e.key === "Escape" && isOpen()) {
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
    }
  }

  function setExpanded(v) {
    if (!anchorBtn) return;
    anchorBtn.setAttribute("aria-expanded", v ? "true" : "false");
  }

  function closeMenu(returnFocus) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    openToken++;
    setExpanded(false);
    document.removeEventListener("pointerdown", onDocPointerDown, true);
    document.removeEventListener("keydown", onDocKeydown, true);
    window.removeEventListener("resize", positionMenu);
    if (returnFocus && anchorBtn) anchorBtn.focus();
  }

  async function openMenu() {
    ensureMenu();
    const token = ++openToken;
    menu.replaceChildren(el("div", "loading", { role: "none" }));
    menu.firstChild.textContent = "Konten werden geladen …";
    menu.hidden = false;
    setExpanded(true);
    positionMenu();
    document.addEventListener("pointerdown", onDocPointerDown, true);
    document.addEventListener("keydown", onDocKeydown, true);
    window.addEventListener("resize", positionMenu);

    // Bei jedem Öffnen neu registrieren (Name/Bild könnten sich geändert haben)
    await register();
    const res = await helper("GET", "/accounts");
    if (token !== openToken) return; // inzwischen geschlossen/neu geöffnet
    render(res);
    positionMenu();
    focusables()[0]?.focus();
  }

  function toggleMenu() {
    if (isOpen()) closeMenu(false);
    else openMenu();
  }

  function initial(name) {
    const s = String(name || "?").trim();
    const ch = Array.from(s)[0] || "?";
    return ch.toUpperCase();
  }

  function avatarNode(acc) {
    const box = el("span", "avatar", { "aria-hidden": "true" });
    const fallback = () => {
      box.replaceChildren();
      box.textContent = initial(acc.displayName || acc.username);
    };
    if (acc.imageUrl) {
      const img = document.createElement("img");
      img.alt = "";
      img.referrerPolicy = "no-referrer";
      img.onerror = fallback;
      img.src = acc.imageUrl;
      box.appendChild(img);
    } else fallback();
    return box;
  }

  function render(res) {
    menu.replaceChildren();
    const header = el("div", "header", { role: "presentation" });
    header.textContent = "Konto wechseln";
    menu.appendChild(header);

    const reachable = !res.unreachable;
    const data = res.data || {};
    const accounts = reachable && Array.isArray(data.accounts) ? data.accounts : [];

    if (!reachable) {
      const hint = el("div", "hint", { role: "note" });
      const strong = document.createElement("strong");
      strong.textContent = "Hilfsdienst läuft nicht";
      const txt = document.createElement("span");
      txt.append("Bitte ");
      const code = document.createElement("code");
      code.textContent = "install.sh";
      const link = document.createElement("a");
      link.href = "https://github.com/JustNotesa/spicetify-account-switcher#installation";
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Anleitung";
      txt.append(code, " ausführen (", link, ").");
      hint.append(strong, txt);
      menu.appendChild(hint);
    } else if (!res.ok) {
      const hint = el("div", "hint", { role: "note" });
      hint.textContent = "Konten konnten nicht geladen werden" + (data.error ? ": " + data.error : ".");
      menu.appendChild(hint);
    } else if (!accounts.length) {
      const hint = el("div", "hint", { role: "note" });
      hint.textContent = "Noch keine Konten gespeichert.";
      menu.appendChild(hint);
    } else {
      const ul = el("ul", "list", { role: "none" });
      const anyCurrent = accounts.some((a) => a && a.current);
      for (const acc of accounts) if (acc && acc.profile) ul.appendChild(accountRow(acc, anyCurrent));
      menu.appendChild(ul);
    }

    menu.appendChild(el("hr", "divider", { role: "separator" }));

    const add = el("button", "item add", {
      type: "button",
      role: "menuitem",
      "data-msw-focus": "",
    });
    const ic = el("span", "add-icon", { "aria-hidden": "true" });
    ic.innerHTML = ICON_PLUS;
    const t = el("span", "texts");
    const n = el("span", "name");
    n.textContent = "Konto hinzufügen";
    t.appendChild(n);
    add.append(ic, t);
    if (!reachable) {
      add.setAttribute("aria-disabled", "true");
      add.title = "Hilfsdienst läuft nicht";
    }
    add.addEventListener("click", () => {
      if (add.getAttribute("aria-disabled") === "true") return;
      doAdd();
    });
    menu.appendChild(add);
  }

  function accountName(acc) {
    return acc.displayName || acc.username || "Neues Profil";
  }

  function accountRow(acc, anyCurrent) {
    // „current“ kommt vom Hilfsdienst; nur falls er keins markiert, über die eigene Registrierung zuordnen.
    const isCurrent = anyCurrent
      ? !!acc.current
      : myProfile
        ? acc.profile === myProfile
        : !!(me && acc.username && me.username === acc.username);
    const loggedIn = acc.loggedIn !== false || isCurrent;
    const name = accountName(acc);
    // Standardprofil und laufende Profile kann der Hilfsdienst nicht entfernen (409) → kein ×.
    const removable = !isCurrent && acc.profile !== "default" && !acc.running;
    const li = el("li", "row" + (isCurrent ? " current" : "") + (removable ? " removable" : ""), { role: "none" });

    const btn = el("button", "item", {
      type: "button",
      role: "menuitemradio",
      "aria-checked": isCurrent ? "true" : "false",
      "data-msw-focus": "",
      "data-profile": acc.profile,
    });
    const texts = el("span", "texts");
    const nm = el("span", "name");
    nm.textContent = name;
    const sub = el("span", "sub");
    if (!loggedIn) {
      sub.textContent = "Nicht angemeldet";
      sub.classList.add(P + "nologin");
    } else {
      sub.textContent = (acc.username || acc.profile) + (isCurrent ? " · aktiv" : "");
    }
    texts.append(nm, sub);
    btn.append(avatarNode(acc), texts);
    if (isCurrent) {
      const ck = el("span", "check", { "aria-hidden": "true" });
      ck.innerHTML = ICON_CHECK;
      btn.appendChild(ck);
    }
    btn.setAttribute(
      "aria-label",
      name +
        (acc.username && acc.username !== name ? " (" + acc.username + ")" : "") +
        (isCurrent ? ", aktuelles Konto" : "") +
        (!loggedIn ? ", nicht angemeldet" : "")
    );
    btn.addEventListener("click", () => {
      if (isCurrent) closeMenu(true);
      else doSwitch(acc);
    });
    li.appendChild(btn);

    if (removable) {
      const rm = el("button", "remove", {
        type: "button",
        "aria-label": name + " entfernen",
        title: "Entfernen",
        "data-msw-focus": "",
      });
      rm.innerHTML = ICON_X;
      rm.addEventListener("click", (e) => {
        e.stopPropagation();
        showRemoveConfirm(li, acc);
      });
      li.appendChild(rm);
    }
    return li;
  }

  function showRemoveConfirm(li, acc) {
    const name = accountName(acc);
    const box = el("div", "confirm", { role: "group", "aria-label": name + " entfernen?" });
    const txt = el("span", "confirm-text");
    txt.textContent = "„" + name + "“ entfernen?";
    const yes = el("button", "pill danger", { type: "button", "data-msw-focus": "", "data-msw-action": "confirm-remove" });
    yes.textContent = "Entfernen";
    const no = el("button", "pill", { type: "button", "data-msw-focus": "", "data-msw-action": "cancel-remove" });
    no.textContent = "Abbrechen";
    box.append(txt, no, yes);
    const old = Array.from(li.childNodes);
    li.replaceChildren(box);
    no.addEventListener("click", (e) => {
      e.stopPropagation();
      li.replaceChildren(...old);
      li.querySelector("." + P + "remove")?.focus();
    });
    yes.addEventListener("click", async (e) => {
      e.stopPropagation();
      yes.disabled = no.disabled = true;
      const res = await helper("POST", "/remove", { profile: acc.profile });
      if (res.ok) {
        notify("„" + name + "“ wurde entfernt.");
        const next = li.nextElementSibling || li.previousElementSibling;
        li.remove();
        const target = (next && next.querySelector("[data-msw-focus]")) || focusables()[0];
        if (target) target.focus();
      } else {
        notify(errorText(res, "Entfernen fehlgeschlagen"), true);
        li.replaceChildren(...old);
      }
    });
    no.focus();
  }

  function errorText(res, prefix) {
    if (res.unreachable) return "Hilfsdienst nicht erreichbar – install.sh ausführen.";
    const msg = res.data && res.data.error;
    return prefix + (msg ? ": " + msg : " (HTTP " + res.status + ")");
  }

  let busy = false;

  const LOGIN_HINT = "Neues Spotify-Fenster öffnet sich – melde dich dort an (Passwort-Anmeldung im Browser).";

  async function doSwitch(acc) {
    if (busy) return;
    const name = accountName(acc);
    busy = true;
    try {
      // Wechsel: Hilfsdienst öffnet das Zielprofil in einem neuen Fenster und schließt dieses, sobald das neue
      // per /register bereit meldet.
      const res = await helper("POST", "/switch", { profile: acc.profile });
      if (res.ok && res.data && res.data.switching === false) {
        notify("„" + name + "“ ist bereits aktiv.");
        closeMenu(true);
      } else if (res.ok) {
        closeMenu(false);
        notify(acc.loggedIn === false ? "Wechsle zu „" + name + "“ … " + LOGIN_HINT : "Wechsle zu „" + name + "“ …");
      } else if (res.status === 404) {
        notify("Profil für „" + name + "“ nicht gefunden. Menü neu öffnen und erneut versuchen.", true);
      } else if (res.status === 409) {
        notify("Ein Kontowechsel läuft bereits.", true);
      } else {
        notify(errorText(res, "Wechsel fehlgeschlagen"), true);
      }
    } finally {
      busy = false;
    }
  }

  async function doAdd() {
    if (busy) return;
    busy = true;
    try {
      const res = await helper("POST", "/add", {});
      if (res.ok) {
        closeMenu(false);
        notify(LOGIN_HINT);
      } else if (res.status === 409) {
        notify("Ein Kontowechsel läuft bereits.", true);
      } else {
        notify(errorText(res, "Konto hinzufügen fehlgeschlagen"), true);
      }
    } finally {
      busy = false;
    }
  }

  // ---- Button in der oberen Leiste ---------------------------------------------------------
  function decorateAnchor(btn) {
    btn.setAttribute("aria-haspopup", "menu");
    btn.setAttribute("aria-controls", "msw-menu");
    btn.setAttribute("aria-expanded", isOpen() ? "true" : "false");
    btn.classList.add(P + "toggle");
  }

  let topbarButton = null;
  let fallbackBtn = null;
  let observer = null;

  function onToggleClick(btn) {
    anchorBtn = btn;
    toggleMenu();
  }

  function createTopbarButton() {
    try {
      if (!(window.Spicetify && Spicetify.Topbar && typeof Spicetify.Topbar.Button === "function")) return null;
      // (label, icon, onClick, disabled, isRight) – isRight → vor den rechten Aktionsknöpfen (.main-actionButtons),
      // also direkt links neben Freundesaktivität/Profilbild.
      const b = new Spicetify.Topbar.Button("Konto wechseln", ICON_PEOPLE, (self) => onToggleClick(self.button), false, true);
      if (b && b.button) {
        decorateAnchor(b.button);
        if (b.element) b.element.classList.add(P + "topbar");
      }
      return b;
    } catch (e) {
      console.warn("[AccountSwitcher] Topbar.Button fehlgeschlagen", e);
      return null;
    }
  }

  function findFallbackSpot() {
    // 1) Spotifys rechte Aktionsknöpfe (Global Nav ab ~1.2.30)
    const actions = document.querySelector(".main-globalNav-contentRight > .main-actionButtons, .main-actionButtons");
    if (actions) return { parent: actions, before: actions.firstChild };
    // 2) direkt vor dem Profil-Widget
    const userBtn =
      document.querySelector(".main-userWidget-box") ||
      document.querySelector('[data-testid="user-widget-link"]');
    if (userBtn && userBtn.parentElement) return { parent: userBtn.parentElement, before: userBtn };
    return null;
  }

  function ensureFallback() {
    if (topbarButton && topbarButton.element && topbarButton.element.isConnected) {
      // Spicetify hat den Button doch noch platziert → Fallback entfernen
      if (fallbackBtn && fallbackBtn.isConnected) fallbackBtn.remove();
      return;
    }
    if (fallbackBtn && fallbackBtn.isConnected) return;
    const spot = findFallbackSpot();
    if (!spot) return;
    if (!fallbackBtn) {
      fallbackBtn = el("button", "fallback-btn", { type: "button", "aria-label": "Konto wechseln", title: "Konto wechseln" });
      fallbackBtn.innerHTML = ICON_PEOPLE;
      decorateAnchor(fallbackBtn);
      fallbackBtn.addEventListener("click", () => onToggleClick(fallbackBtn));
    }
    spot.parent.insertBefore(fallbackBtn, spot.before || null);
  }

  function startFallbackObserver() {
    if (observer) return;
    let scheduled = false;
    observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        ensureFallback();
      });
    });
    observer.observe(document.body, { childList: true, subtree: true });
    ensureFallback();
  }

  // ---- Start -------------------------------------------------------------------------------
  async function main() {
    await waitFor(() => window.Spicetify && document.body && Spicetify.Platform && Spicetify.showNotification, {
      interval: 100,
    });
    injectStyle();

    // Sofort (parallel zum Button-Aufbau) beim Hilfsdienst melden: Das ist das Bereit-Signal, auf das ein
    // laufender Wechsel wartet, bevor er das alte Fenster schließt.
    registerOnStartup().catch((e) => console.warn("[AccountSwitcher] Registrierung fehlgeschlagen", e));

    // Topbar-API bis zu 10 s abwarten, sonst DOM-Fallback
    await waitFor(() => Spicetify.Topbar && Spicetify.Topbar.Button, { timeout: 10000 });
    topbarButton = createTopbarButton();
    if (topbarButton) anchorBtn = topbarButton.button;

    // Spicetify hängt den Button erst ein, wenn es die Leiste findet. Klappt das nicht in 8 s → Fallback.
    const attached = topbarButton
      ? await waitFor(() => topbarButton.element && topbarButton.element.isConnected, { interval: 250, timeout: 8000 })
      : false;
    if (!attached) {
      console.info("[AccountSwitcher] Topbar-Button nicht eingehängt – nutze DOM-Fallback");
      startFallbackObserver();
    }
  }

  // Test-Handle
  window.__mswAccountSwitcher = {
    openMenu,
    closeMenu,
    register,
    helperUrl: HELPER,
    get profile() {
      return myProfile;
    },
  };

  main().catch((e) => console.error("[AccountSwitcher]", e));
})();
