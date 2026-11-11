# Spotify Multi-Account Switcher – Architektur & Schnittstelle v2

## Idee (v2: ein Spotify-Profil je Konto)
Spotify Desktop (macOS 1.2.63, Spicetify 2.46.4) kann mit `--mu=<id>` mehrfach laufen, jede Instanz mit eigenem
Datenordner `~/Library/Application Support/Spotify-<id>/` (eigene `prefs` inkl. eigener gespeicherter Anmeldung)
und eigenem Cache `~/Library/Caches/com.spotify.client-<id>/`. Ohne `--mu` = Standardprofil `default`
(Ordner `~/Library/Application Support/Spotify/`). **Live verifiziert:** ein angemeldetes `--mu`-Profil meldet sich
beim Start selbst an, UI bereit nach ~1,5 s. Kopierte `autologin.*`-Zeilen werden in einem anderen Profil NICHT
angenommen → jedes Profil meldet sich einmal selbst an. **`autologin.*` wird nie gelesen, kopiert oder geändert**
(nur `canonical_username` lesen, um Profil→Konto zuzuordnen).

Wechsel (Fenster bleibt sichtbar, nur kurz zwei Instanzen):
1. Ziel-Profil starten (`open -n -a /Applications/Spotify.app --args --mu=<id>`; für `default` ohne Argumente),
   vorher Fensterposition (`app.window.position.*`-Zeilen) vom laufenden Profil in die prefs des Ziels übernehmen
   (nur wenn Ziel nicht läuft; atomar; andere Zeilen unverändert).
2. Warten bis die Erweiterung im Ziel `/register` meldet (= UI bereit), max. 20 s.
3. Dann die alte Instanz sanft beenden (SIGTERM auf ihre Haupt-PID; nach 8 s SIGKILL). Kommt kein `/register`,
   bleibt die alte Instanz laufen (Ziel zeigt evtl. Anmeldebildschirm).
Speicher: außerhalb des Wechsels läuft immer nur eine Instanz.

## Teile
1. `helper/spotify_accounts_helper.py` – Python-3.9-Stdlib-HTTP-Dienst, `127.0.0.1:48655`,
   **launchd-Socket-Aktivierung** (startet erst bei der ersten Verbindung, beendet sich nach 60 s ohne Anfrage
   und ohne laufenden Wechsel). Für Tests/CLI auch ohne launchd startbar (bindet dann selbst).
2. `extension/accountSwitcher.js` – Spicetify-Erweiterung (Button + Menü).
3. `install.sh` / `uninstall.sh`.

## Datenablage des Helpers
`~/Library/Application Support/spotify-multiaccount/` (chmod 700): `profiles.json`, `helper.log`.
`profiles.json` = `{"profiles": {"<id>": {"username": str|null, "displayName": str|null, "imageUrl": str|null,
"createdAt": iso, "updatedAt": iso}}}`.
Profile werden zusätzlich per Scan gefunden: `default` (falls `Spotify/prefs` existiert) und jeder Ordner
`Spotify-<id>` mit Datei `prefs`, wobei `<id>` ~ `^[a-z0-9][a-z0-9_-]{0,39}$` (also NICHT `Spotify-Accounts`,
`Spotify-Profiles`, „Some Other App“ – Ordner mit Großbuchstaben ignorieren).
Neue Profil-IDs: `a` + UTC `YYYYMMDDHHMMSS`.

Umgebungsvariablen (Tests): `SPOTIFY_APPSUPPORT` (statt `~/Library/Application Support`), `SPOTIFY_CACHES`
(statt `~/Library/Caches`), `SPOTIFY_MA_DIR`, `SPOTIFY_HELPER_DRYRUN=1` (nichts starten/beenden/öffnen, nur loggen),
`SPOTIFY_HELPER_PORT`, `SPOTIFY_HELPER_OPEN_BROWSER=0`, `SPOTIFY_HELPER_IDLE=<s>` (0 = nie beenden),
`SPOTIFY_TRASH` (statt `~/.Trash`), `SPOTIFY_HELPER_NO_LAUNCHD=1` (keine Socket-Aktivierung versuchen, selbst binden).

## Laufende Instanzen erkennen
`ps -axww -o pid=,args=` (`ww` = Argumente nicht abschneiden): Zeilen, deren Programm genau `/Applications/Spotify.app/Contents/MacOS/Spotify` ist.
`--mu=<id>` in den Argumenten → Profil `<id>`, sonst `default`.

## HTTP-API (JSON, Port 48655)
CORS nur für Origin `https://xpui.app.spotify.com` (+ `Access-Control-Allow-Private-Network: true`), fremder
Origin → 403, ohne Origin erlaubt, Preflight beantworten, Host-Header nur 127.0.0.1/localhost/[::1].

| Methode | Pfad | Body | Antwort |
|---|---|---|---|
| GET | `/health` | – | `{"ok":true,"version":"2.0.0"}` |
| GET | `/accounts` | – | `{"accounts":[{"profile","username","displayName","imageUrl","running":bool,"current":bool,"loggedIn":bool}]}` – `current` = läuft (bei mehreren: das zuletzt per `/register` gemeldete); `loggedIn` = prefs hat `autologin.canonical_username`. Sortierung: current zuerst, dann displayName. |
| POST | `/register` | `{"username","displayName","imageUrl"}` | Profil per `canonical_username`-Scan der prefs bestimmen (bei Treffern in mehreren Profilen: das laufende). Metadaten speichern. Gilt als „bereit“-Signal für laufende Wechsel. → `{"ok":true,"profile":"<id>"}`; kein Profil gefunden → 404 |
| POST | `/switch` | `{"profile"}` | unbekannt → 404; Ziel == current → `200 {"ok":true,"switching":false}`; sonst `202 {"ok":true,"switching":true}` + Wechsel wie oben im Hintergrund |
| POST | `/add` | `{}` / `{"openBrowser":false}` | neues Profil anlegen (in profiles.json), mit `--mu=<neu>` starten, nach ~2 s Browser mit `loginUrl` öffnen. Alte Instanz bleibt, bis das neue Profil `/register` meldet (max. 10 min), dann beenden. → `202 {"ok":true,"profile":"<neu>","loginUrl":"…"}` |
| POST | `/remove` | `{"profile"}` | nicht `default`, nicht laufend (sonst 409), unbekannt 404. Ordner `Spotify-<id>` und `com.spotify.client-<id>` in den Papierkorb verschieben (`~/.Trash/<name>-<zeitstempel>`), Eintrag löschen → `{"ok":true}` |

Nur ein Wechsel/Hinzufügen gleichzeitig → sonst 409. Fehler: `{"ok":false,"error":"…"}`.

`loginUrl`: `https://accounts.spotify.com/logout?continue=` + `quote(INNER, safe='')`,
`INNER = "https://accounts.spotify.com/de/login?allow_password=1&continue=" + quote("https://accounts.spotify.com/de/status", safe='')`.

## Umsetzungsdetails Helper (Ergänzungen, ändern die API-Form nicht)
- LaunchAgent-Label `com.spotify-multiaccount.switcher` (v1-Label `com.spotify-multiaccount.helper` wird von
  `install.sh` entfernt); Daten des v1-Ordners `Spotify-Accounts` werden nicht übernommen und nicht angefasst.
- Scan ignoriert zusätzlich `Spotify-default` (die ID `default` ist für den Ordner `Spotify` reserviert).
  Einträge aus `profiles.json` ohne Ordner (z. B. frisch per `/add` angelegt) erscheinen ebenfalls in `/accounts`.
- `/register`-Zuordnung, falls `canonical_username` in keiner prefs steht (Spotify schreibt die Anmeldung evtl.
  erst später): unter den laufenden Profilen ohne Anmeldung in den prefs → das Ziel des laufenden Wechsels/Hinzufügens,
  sonst eins mit diesem Benutzernamen in `profiles.json`, sonst das einzige solche; sonst 404.
- Fensterposition wird nur in eine schon vorhandene prefs-Datei übernommen (der Helper legt keine Spotify-Ordner an,
  bei `/add` eines neuen Profils also keine Übernahme).
- `/switch`: läuft gar keine Instanz → Ziel nur starten, nicht warten. Läuft das Ziel schon (zweite Instanz) →
  nicht neu starten, sofort die alte beenden. Wird das Ziel während des Wartens geschlossen → Abbruch, alte bleibt.
  Ungültige Profil-ID (kein String / passt nicht zur Regex) → 400. Gestartet wird immer mit `open -n -a`.
- `/add`: wartet auch ohne laufende alte Instanz auf `/register` (bzw. Abbruch/10 min), damit die Zuordnung klappt.
- `/remove` von `default` → 409. Zeitstempel im Papierkorb-Namen: UTC `YYYYMMDD-HHMMSS` (bei Kollision `-2`, `-3` …).
- SIGTERM/SIGKILL nur an PIDs, die beim Wechsel-Start als Haupt-PID des alten Profils gesehen wurden und es beim
  Signal noch sind (Schutz vor PID-Wiederverwendung). DRYRUN: kein Start/Signal/Browser, nur Log.
