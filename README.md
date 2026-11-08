# Debian-Updates über SSH – für Systeme ohne Internetzugang

Für ein Zielsystem, das **nur per SSH (Port 22) erreichbar** ist und **selbst
nicht ins Internet kommt**. Ein Rechner mit Internet (das *Gateway*, z. B. dein
Arbeitsplatz) stellt dem Ziel für die Dauer des Updates einen Paket-Proxy
bereit, und zwar ausschließlich über die SSH-Verbindung, die ohnehin besteht.

```
 Gateway (hat Internet)                         Zielsystem (kein Internet)
┌──────────────────────────────┐   SSH :22    ┌──────────────────────────────┐
│ apt_proxy.py  127.0.0.1:L    │◄═════════════│ 127.0.0.1:R  (Reverse-Tunnel)│
│   │  Whitelist + Paket-Cache │   ssh -R     │   ▲                          │
│   ▼                          │              │   │                          │
│ deb.debian.org, security...  │              │ apt-get -o Acquire::http::   │
└──────────────────────────────┘              │         Proxy=127.0.0.1:R    │
                                              └──────────────────────────────┘
```

Es wird **kein Port geöffnet**, **keine Datei auf dem Ziel geändert** (der
Proxy wird per `apt-get -o …` übergeben), und nach dem Lauf sind Tunnel und
Proxy wieder weg.

## Inhalt

| Datei | Zweck |
|---|---|
| `bin/apt-via-ssh` | **Hauptwerkzeug**: Proxy starten, Reverse-Tunnel aufbauen, apt auf dem Ziel ausführen, aufräumen |
| `gateway/apt_proxy.py` | HTTP-Proxy (nur Python-Standardbibliothek): Host-Whitelist, CONNECT für https, Cache für `.deb`-Dateien |
| `bin/apt-offline-sync` | **Fallback**, falls das Ziel keine Port-Weiterleitung erlaubt (`AllowTcpForwarding no`) |
| `etc/apt-via-ssh.conf.example` | Beispielkonfiguration |
| `tests/rauchtest.sh` | Rauchtest, der das Ziel nicht verändert |
| `tests/docker-test.sh` | Integrationstest gegen echte, abgeschottete Debian-Container |

## Schnellstart

```bash
# 1. Verbindung und Tunnel prüfen (lädt nur Paketlisten)
bin/apt-via-ssh root@10.0.0.5 selftest

# 2. Was steht an?
bin/apt-via-ssh root@10.0.0.5 check

# 3. Aktualisieren
bin/apt-via-ssh root@10.0.0.5 upgrade          # mit Rückfrage
bin/apt-via-ssh -y root@10.0.0.5 full-upgrade  # unbeaufsichtigt
```

Als Nicht-root-Benutzer wird automatisch `sudo -n` verwendet. Dafür braucht der
Benutzer auf dem Ziel `NOPASSWD` für apt-get.

### Befehle

| Befehl | Wirkung |
|---|---|
| `selftest` | Verbindung, Tunnel und `apt-get update` prüfen |
| `check` | Paketlisten holen, anstehende Updates auflisten |
| `update` | nur `apt-get update` |
| `upgrade` / `full-upgrade` | update + upgrade bzw. full-upgrade |
| `install PAKET …` | update + install |
| `download PAKET …` | nur herunterladen (für ein späteres Wartungsfenster) |
| `run BEFEHL …` | beliebiger Befehl mit gesetztem `http_proxy`/`https_proxy`; alles nach `run` wird unverändert weitergegeben, ein einzelnes Argument läuft als Shell-Skript (`run 'a \| b'`) |
| `tunnel` | Tunnel offen halten und selbst auf dem Ziel arbeiten (bis Strg-C) |

Wichtige Optionen: `-y` (ohne Rückfrage), `-s` (nur simulieren), `-d` (nur
laden), `--allow HOST` (weiteres Repository freigeben), `-p PORT`, `-i SCHLÜSSEL`,
`-o SSH-OPTION`, `--use-proxy 127.0.0.1:3142` (z. B. vorhandenes apt-cacher-ng),
`-v`. Vollständig: `bin/apt-via-ssh --help`.

### Drittanbieter-Repositories

Standardmäßig leitet der Proxy nur an `*.debian.org`, `*.debian.net` und Raspbian
weiter. Nutzt das Ziel weitere Quellen (Docker, Microsoft …), zeigt das Skript am
Ende genau an, was blockiert wurde, samt fertigem Aufruf:

```
Folgende Repository-Hosts wurden von der Whitelist blockiert:
  download.docker.com
Freigeben mit: --allow download.docker.com
```

Dauerhaft geht das über `ERLAUBTE_HOSTS` in `~/.config/apt-via-ssh.conf`.

### Ziel nur über einen Jump-Host erreichbar

Funktioniert unverändert, der Reverse-Tunnel läuft durch:

```bash
bin/apt-via-ssh -o ProxyJump=admin@bastion root@10.0.0.5 upgrade
```

Ein Alias aus `~/.ssh/config` geht ebenso.

### Automatisieren (z. B. Cron auf dem Gateway)

```cron
# jeden Sonntag 03:00 Sicherheitsupdates einspielen
0 3 * * 0  /pfad/bin/apt-via-ssh -o BatchMode=yes -y root@10.0.0.5 upgrade >>/var/log/apt-via-ssh.log 2>&1
```

## Fallback: `apt-offline-sync` (ohne Port-Weiterleitung)

Ist auf dem Ziel `AllowTcpForwarding no` gesetzt, meldet `apt-via-ssh` das und
verweist hierher. `apt-offline-sync` braucht nur die Möglichkeit, Befehle
auszuführen:

1. holt vom Ziel Paketquellen, Keyrings, dpkg-Status und Architektur,
2. lässt `apt` auf dem Gateway **stellvertretend für das Ziel** rechnen (eigenes
   Wurzelverzeichnis, ohne root). Die Signaturen prüft es dabei mit den
   **Keyrings des Ziels**, und es lädt genau die nötigen `.deb`-Dateien,
3. überträgt Listen und Pakete per `tar` über die SSH-Sitzung,
4. installiert auf dem Ziel mit `apt-get --no-download`.

```bash
bin/apt-offline-sync root@10.0.0.5 check
bin/apt-offline-sync -y root@10.0.0.5 upgrade
bin/apt-offline-sync root@10.0.0.5 install htop
bin/apt-offline-sync --stage-only root@10.0.0.5 full-upgrade   # nur bereitstellen
```

Voraussetzung: Das Gateway ist selbst ein Debian/Ubuntu mit `apt` und `gpgv`.
Die bisherigen Paketlisten des Ziels werden nach
`/var/lib/apt/lists.vor-apt-offline-sync` gesichert. Nicht unterstützt:
`file:`/`cdrom:`-Quellen und Einstellungen aus `/etc/apt/apt.conf.d` des Ziels
(Pinning in `preferences` wird übernommen).

## Sicherheit

- Der Proxy lauscht nur auf `127.0.0.1` des Gateways. Der Reverse-Tunnel endet
  auf dem Ziel ebenfalls auf `127.0.0.1` (sshd-Vorgabe `GatewayPorts no`), und
  zwar nur, solange das Skript läuft.
- Die **Whitelist** verhindert, dass das Ziel über den Tunnel beliebig ins Internet
  kommt. `CONNECT` (https) ist nur zu Port 443 erlaubt.
- Debian-Repositories über `http://` sind unbedenklich: apt prüft jede Datei
  gegen die signierten `InRelease`-Dateien. Ein manipulierter Proxy oder Cache
  fällt also auf.
- Gecacht werden nur unveränderliche Pfade (`pool/`, `by-hash/`, `*.deb`), nie
  Index-Dateien. Ein Fehler beim Cachen (z. B. volle Platte) bricht die
  Übertragung zum Ziel nicht ab.

## Voraussetzungen

| | |
|---|---|
| Gateway | Python ≥ 3.9, OpenSSH-Client, Internetzugang; für den Fallback zusätzlich apt + gpgv |
| Ziel | sshd (für `apt-via-ssh` mit `AllowTcpForwarding yes`, Vorgabe), apt, root oder `sudo` ohne Passwort |

## Fehlersuche

| Meldung | Ursache / Abhilfe |
|---|---|
| `Der Reverse-Tunnel konnte nicht aufgebaut werden` | `AllowTcpForwarding no` auf dem Ziel → `apt-offline-sync` nutzen |
| `sudo ohne Passwort ist … nicht möglich` | als root verbinden oder `NOPASSWD` für apt-get einrichten |
| `403 Forbidden` / `Invalid response from proxy` | Host nicht in der Whitelist → `--allow HOST` |
| `Proxy ist vom Zielsystem aus nicht erreichbar` | mit `-v --proxy-debug` wiederholen |

## Tests

Rauchtest gegen ein beliebiges Ziel (verändert es nicht, nur `update` und Simulationen):

```bash
tests/rauchtest.sh root@10.0.0.5
```

Integrationstest mit Docker: Er baut aus einem älteren Debian-Image (damit echte
Updates anstehen) ein Ziel mit sshd, ohne curl, wget und python3, und startet zwei Container in einem Docker-Netz **ohne Internet**
(`--internal`). Geprüft werden:

- dass die Container wirklich keine Verbindung nach draußen haben,
- echte Upgrades und Installationen über `apt-via-ssh`, als root und als
  Benutzer mit `sudo`,
- der Paket-Cache, `run` und das Aufräumen,
- ein zweiter Container mit `AllowTcpForwarding no`: verständliche
  Fehlermeldung, danach Upgrade und Installation über `apt-offline-sync`.

```bash
tests/docker-test.sh                   # Ziel: Debian 12 vom Mai 2024, viele Updates offen
```
```bash
DEBIAN_TAG=trixie tests/docker-test.sh  # Ziel: aktuelles Debian 13
```

Container und Netz werden danach entfernt, das Image bleibt für Wiederholungen.
