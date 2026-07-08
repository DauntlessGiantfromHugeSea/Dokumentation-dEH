# Erste-Hilfe Camp-Dokumentation

Webinterface zur Erfassung von Erste-Hilfe-Leistungen gemäß DGUV
Information 1. Ersetzt das Papierformular durch eine durchsuchbare
Online-Datenbank, erkennt Folgebehandlungen automatisch über Name +
Geburtsdatum und exportiert einzelne Einsatzberichte als PDF sowie
gefilterte Datensätze als CSV.

## Features

- Login (Benutzerkonten, Passwort-Hash via Werkzeug)
- Anlegen, Anzeigen und Bearbeiten von Einsatzberichten
- Automatische Erkennung von Folgebehandlungen (Name + Geburtsdatum)
- Kommentare je Bericht
- PDF-Export einzelner Berichte (`Einsatzbericht #<ID>`)
- CSV-Export gefilterter Datensätze (Datum, Stammnummer, Name)
- Personenübersicht mit allen Folgebehandlungen je Person

## Lokal starten

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Ersten User anlegen
export FLASK_APP=app.py
flask create-user admin --full-name "Camp-Leitung"

# Server starten
python app.py
```

Aufrufen: http://localhost:8000/

## Konfiguration (Umgebungsvariablen)

| Variable      | Default          | Beschreibung                                  |
| ------------- | ---------------- | --------------------------------------------- |
| `SECRET_KEY`  | `dev-only-...`   | Flask-Sitzungsschlüssel — **in Produktion setzen!** |
| `DB_PATH`     | `data/app.db`    | Pfad zur SQLite-Datei                         |
| `PORT`        | `8000`           | HTTP-Port                                     |

### frodor-Anbindung (optional)

Verbindet die App mit den Camp-Anmeldungen der frodor-Plattform: Personen
lassen sich beim Anlegen eines Berichts aus den Anmeldungen suchen
(Notfallkontakt/Allergien/Medikamente werden übernommen), und fertige
Protokolle können als PDF an die Anmeldung übertragen werden
(Detailseite → „An frodor übertragen", Status unter `/frodor/uploads`).
Ohne diese Variablen ist die Anbindung komplett deaktiviert.

| Variable                   | Default       | Beschreibung                                |
| -------------------------- | ------------- | ------------------------------------------- |
| `FRODOR_SUPABASE_URL`      | —             | Supabase-URL, z. B. `https://<ref>.supabase.co` |
| `FRODOR_SUPABASE_ANON_KEY` | —             | anon/publishable Key des Projekts           |
| `FRODOR_EMAIL`             | —             | Service-Account (Rolle „Sanitätsdokumentation") |
| `FRODOR_PASSWORD`          | —             | Passwort des Service-Accounts               |
| `FRODOR_EVENT_SLUG`        | `dc-ost-2026` | Event, dessen Anmeldungen genutzt werden    |

## Deployment (VPS + Tailscale)

Empfohlenes Setup: Docker-Container auf dem eigenen VPS, **nicht öffentlich
erreichbar** — Zugriff ausschließlich über Tailscale (WireGuard-VPN). Nur
Geräte, die explizit ins Tailnet aufgenommen wurden, erreichen die App.
Ein Fehler in der App ist damit aus dem Internet nicht ausnutzbar, weil
niemand aus dem Internet den Port erreicht.

### 1. Container starten

Das Image wird bei jedem Push auf `main` automatisch nach ghcr gebaut
(`.github/workflows/deploy.yaml`) und liegt unter
`ghcr.io/dauntlessgiantfromhugesea/camp-doku`.

```bash
git clone <repo> /opt/camp-doku && cd /opt/camp-doku
cp deploy.env.example deploy.env   # Werte setzen (SECRET_KEY, FRODOR_*, Backup-Key)

# Bei privatem Repo einmalig: PAT mit read:packages
docker login ghcr.io -u <github-user>

docker compose -f deploy.compose.yaml up -d

# Ersten User anlegen (wird automatisch Admin)
docker compose -f deploy.compose.yaml exec app \
  flask create-user admin --full-name "Camp-Leitung"
```

Update einspielen (bewusst manuell, Watchtower ist für diesen Container
deaktiviert):

```bash
docker compose -f deploy.compose.yaml pull && docker compose -f deploy.compose.yaml up -d
```

Der Container bindet bewusst nur an `127.0.0.1:8010` (kein Traefik-Label,
kein öffentlicher Port). Die SQLite-DB liegt als Bind-Mount unter `./data/`.

### 2. Tailscale davor

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# HTTPS im Tailnet (echtes Zertifikat unter https://<host>.<tailnet>.ts.net;
# HTTPS wird für PWA/Service-Worker benötigt). HTTPS-Zertifikate müssen im
# Admin-Panel einmalig aktiviert sein (DNS → HTTPS Certificates → Enable).
sudo tailscale serve --bg 8010
```

Auf jedem erlaubten Gerät (Sani-Tablets): Tailscale-App installieren und
ins selbe Tailnet einloggen. Für geteilte Camp-Geräte besser pro Gerät
einen Auth-Key mit Tag ausstellen (Admin-Panel → Settings → Keys),
z. B. `tag:sani` — dann lässt sich der Zugriff per ACL einschränken:

```jsonc
// Tailscale Admin → Access Controls
{
  "tagOwners": { "tag:sani": ["autogroup:admin"] },
  "acls": [
    { "action": "accept", "src": ["tag:sani", "autogroup:admin"],
      "dst": ["<vps-hostname>:443"] }
  ]
}
```

Gerät verloren/ausgemustert → im Tailscale-Admin entfernen, Zugriff ist
sofort weg (zusätzlich App-Login + TOTP als zweite Schicht).

### 3. Backups (verschlüsselt, off-server entschlüsselbar)

Einmalig auf dem **eigenen Laptop** (nicht auf dem Server):

```bash
age-keygen -o camp-doku-backup-key.txt   # sicher verwahren!
# den "public key: age1..." in deploy.env als BACKUP_AGE_RECIPIENT eintragen
```

Auf dem VPS (`apt install sqlite3 age`), Cron stündlich:

```
0 * * * * /opt/camp-doku/scripts/backup.sh >> /var/log/camp-doku-backup.log 2>&1
```

Der Server kann Backups nur **erzeugen**, nicht entschlüsseln (nur der
private Schlüssel auf dem Laptop kann das). Wiederherstellen:

```bash
age -d -i camp-doku-backup-key.txt -o app.db backups/app-<zeitstempel>.db.age
```

Optional `BACKUP_RSYNC_TARGET` in `deploy.env` setzen, um die Backups
zusätzlich auf einen zweiten Host zu spiegeln.

### 4. Nach dem Camp (Einmal-Nutzung)

1. Letztes Backup ziehen und lokal verifizieren (`age -d … && sqlite3 app.db "PRAGMA integrity_check;"`).
2. Container stoppen: `docker compose -f deploy.compose.yaml down`.
3. `data/` und `backups/` vom Server löschen; verschlüsseltes Archiv gemäß
   Löschkonzept/Aufbewahrungsfrist beim Verantwortlichen verwahren.
4. frodor-Service-Account entziehen: Rückbau-Block in
   `frodor-supabase/scripts/setup_sanitaetsdokumentation.sql` ausführen
   und den Auth-User im Supabase-Dashboard löschen.
5. Tailnet aufräumen (Camp-Geräte + Auth-Keys entfernen).

## Weitere Benutzer anlegen

```bash
flask create-user vorname.nachname --full-name "Vorname Nachname"
```

## Datenmodell

- `users`: Benutzerkonten
- `patients`: Personen, eindeutig über `(name, geburtsdatum)`
- `protocols`: Einsatzberichte, verknüpft mit Patient und User
- `comments`: Kommentare je Bericht
