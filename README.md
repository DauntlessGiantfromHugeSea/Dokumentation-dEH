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

## Online-Deployment

### Docker

```bash
docker build -t camp-doku .
docker run -d \
  -p 8000:8000 \
  -v /pfad/zu/persistentem/volume:/data \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  --name camp-doku camp-doku

# Ersten User im laufenden Container anlegen
docker exec -it camp-doku flask create-user admin --full-name "Camp-Leitung"
```

Die SQLite-Datei liegt unter `/data/app.db` und sollte über ein
persistentes Volume gesichert werden.

### Hosting-Empfehlungen

- **Fly.io** oder **Railway**: Dockerfile direkt deployen, Volume für `/data`.
- **Eigener VPS**: Container mit nginx als TLS-Reverse-Proxy davorschalten.
- **Backup**: Regelmäßig `app.db` kopieren (z. B. `cron` + `sqlite3 .backup`).

## Weitere Benutzer anlegen

```bash
flask create-user vorname.nachname --full-name "Vorname Nachname"
```

## Datenmodell

- `users`: Benutzerkonten
- `patients`: Personen, eindeutig über `(name, geburtsdatum)`
- `protocols`: Einsatzberichte, verknüpft mit Patient und User
- `comments`: Kommentare je Bericht
