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
| `start.sh` | **Startskript der Oberfläche** im Projektverzeichnis (prüft Python und OpenSSH, funktioniert auch über einen Symlink) |
| `bin/apt-via-ssh-gui` | **Oberfläche im Browser**: Ziel anmelden, Voraussetzungen einrichten, Updates mit Vorschau, Distributions-Upgrade |
| `bin/apt-via-ssh` | **Kommandozeile**: Proxy starten, Reverse-Tunnel aufbauen, apt auf dem Ziel ausführen, aufräumen |
| `gateway/apt_proxy.py` | HTTP-Proxy (nur Python-Standardbibliothek): Host-Whitelist, CONNECT für https, Cache für `.deb`-Dateien |
| `bin/apt-offline-sync` | **Fallback**, falls das Ziel keine Port-Weiterleitung erlaubt (`AllowTcpForwarding no`) |
| `ziel/apt-via-ssh-helper` | Hilfsskript auf dem Ziel: die einzige Datei, die der Wartungsbenutzer per sudo ausführen darf |
| `ziel/einrichten.sh`, `ziel/entfernen.sh` | richten den Wartungszugang auf dem Ziel ein bzw. entfernen ihn wieder |
| `etc/apt-via-ssh.conf.example` | Beispielkonfiguration |
| `tests/rauchtest.sh` | Rauchtest, der das Ziel nicht verändert |
| `tests/docker-test.sh` | Integrationstest gegen echte, abgeschottete Debian-Container |

## Oberfläche

```bash
./start.sh
```

Optionen: `--kein-browser` (nur den Link ausgeben), `--port 9000` (Vorgabe 8765;
ist der Port belegt, wird ein freier gewählt). Für einen Aufruf von überall:

```bash
ln -s "$PWD/start.sh" ~/bin/apt-via-ssh-gui
```

Das Skript startet einen kleinen Webserver **nur auf 127.0.0.1** und öffnet den Browser.
Er braucht nur die Python-Standardbibliothek, keine Installation.

1. **Ziel und Anmeldung**: IP oder Hostname, Port, Benutzer, Passwort und/oder
   SSH-Schlüssel (mit Passphrase), optional ein SSH-Zertifikat. Fehlen dem
   Benutzer sudo-Rechte, kann zusätzlich das root-Passwort für `su` angegeben
   werden. Beim ersten Kontakt zeigt die Oberfläche die Fingerabdrücke des
   Hostschlüssels zur Bestätigung an. Ein *geänderter* Hostschlüssel wird
   deutlich als Warnung gezeigt.
2. **Voraussetzungen**: prüft Rechte, Tunnel, Speicherplatz, sudo und sshd und
   richtet mit einem Klick den **Wartungsbenutzer** ein (Vorgabe `aptpflege`):
   - eigener SSH-Schlüssel, kein Passwort; der Schlüssel ist mit `restrict`
     eingeschränkt (kein Terminal, Tunnel nur auf localhost)
   - `sudo` **nur** für `/usr/local/sbin/apt-via-ssh-helper`
   - sshd-Regel nur für diesen Benutzer: ausschließlich Reverse-Tunnel, keine
     Weiterleitung ins Netz des Ziels (schaltet den Tunnel frei, wenn
     `AllowTcpForwarding` global gesperrt ist)
   - fehlt `sudo`, wird es über den Tunnel installiert, notfalls in einem
     zweiten Durchgang über den Tunnel des neuen Wartungsbenutzers

   Danach verbindet man sich als Wartungsbenutzer, ohne Passwort.
3. **Updates**: Paketlisten holen, verfügbare Updates mit Sicherheits-Kennzeichnung
   anzeigen, einzeln oder alle auswählen. **Vor jeder Installation** zeigt eine
   Vorschau genau, was aktualisiert, neu installiert und **entfernt** wird. Erst
   nach „OK“ geht es los. Außerdem: Pakete installieren, `autoremove`.
4. **Installiert**: alle installierten Pakete mit Filter.
5. **Distributions-Upgrade** (z. B. 12 „bookworm“ → 13 „trixie“):
   - Vorab-Prüfung (dpkg-Zustand, Platz, offene Updates, gehaltene Pakete)
   - **Simulation** gegen die Paketlisten des neuen Release in einem
     Testverzeichnis. Am System ändert sich dabei nichts.
   - Start nur mit Bestätigung. Die Quellen werden gesichert, scheitert der
     Download, werden sie automatisch zurückgesetzt.
   - danach Neustart per Knopf

**Abgekoppelte Installation:** Pakete werden zuerst über den Tunnel geladen,
danach installiert `dpkg` auf dem Ziel in einer eigenen Sitzung (`setsid`). Reißt
die SSH-Verbindung ab, läuft die Installation weiter. Nach dem Neuverbinden lässt
sie sich mit „verfolgen“ wieder aufnehmen.

Gespeichert werden in `~/.config/apt-via-ssh/` die Ziele (ohne Passwörter), die
known_hosts der Oberfläche und die Wartungsschlüssel. Passwörter bleiben nur im
Arbeitsspeicher. An `ssh` gelangen sie über `SSH_ASKPASS`: eine Datei mit Modus 0600, die
sofort nach der Anmeldung gelöscht wird.

Der Wartungsbenutzer funktioniert auch mit der Kommandozeile, z. B. per Cron:

```bash
bin/apt-via-ssh -y -i ~/.config/apt-via-ssh/schluessel/10.0.0.5_22_aptpflege_ed25519 aptpflege@10.0.0.5 upgrade
```

## Schnellstart (Kommandozeile)

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

Der Wartungsbenutzer erhält bewusst kein `sudo` für `apt-get` selbst, denn über
`apt-get -o APT::Update::Pre-Invoke::=…` ließe sich jeder Befehl als root
ausführen. Das Hilfsskript bietet stattdessen feste Befehle an, prüft Proxy-Adresse
und Paketnamen und kann Paketquellen nur für das Distributions-Upgrade umstellen
(Codename alt → neu). Neue Quellen oder Schlüssel kann es nicht hinzufügen. apt
prüft weiterhin alle Signaturen.

Die Oberfläche selbst lauscht nur auf 127.0.0.1. Sie prüft den Host-Header (gegen
DNS-Rebinding) und verlangt ein zufälliges Sitzungs-Token im Cookie **und** im
Header (gegen CSRF). Fremde Skripte lädt sie nicht (Content-Security-Policy).

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
| Oberfläche: „Hilfsskript … Version 1 statt 2“ | mit dem Erstzugang verbinden und „Einrichten“ erneut ausführen; das aktualisiert nur, was fehlt |
| nach einem Upgrade auf Debian 13: neue SSH-Verbindungen scheitern, sshd meldet `-R not supported here` | der alte sshd läuft noch und startet das neue OpenSSH 10. Normalerweise startet das Paket sshd selbst neu; sonst auf dem Ziel `systemctl restart ssh`. Bestehende Verbindungen sind nicht betroffen |
| Oberfläche meldet Verbindungsabbruch während einer Installation | die Installation läuft auf dem Ziel weiter: neu verbinden und in der Systemzeile „verfolgen“ klicken |

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

End-to-End-Test der Oberfläche über ihre API. Dafür laufen zwei abgeschottete Ziele:
eines mit sudo *mit* Passwort, eines ohne sudo, mit gesperrter Weiterleitung und
root nur über su. Getestet werden Einrichtung, Updates, Vorschau, ein
Verbindungsabbruch mitten in der Installation, der CLI-Helfer-Modus, das Entfernen
und ein **echtes Distributions-Upgrade 12 → 13**:

```bash
python3 tests/gui-test.py              # ca. 5–10 Minuten
```
```bash
python3 tests/gui-test.py --ohne-dist  # schneller, ohne Distributions-Upgrade
```

## Lizenz

MIT – siehe [LICENSE](LICENSE). Die Software wird ohne Gewähr bereitgestellt:
Vor dem Einsatz auf produktiven Systemen bitte ein Backup bzw. einen Snapshot anlegen.
