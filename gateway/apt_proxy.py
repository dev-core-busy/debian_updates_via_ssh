#!/usr/bin/env python3
"""
Minimaler HTTP-Forward-Proxy fuer APT -- nur Python-Standardbibliothek.

Laeuft auf dem Gateway (Rechner MIT Internetzugang) und lauscht
standardmaessig nur auf 127.0.0.1. Erreichbar wird er fuer das
abgeschottete Zielsystem ausschliesslich ueber einen SSH-Reverse-Tunnel
(ssh -R), d.h. es wird kein zusaetzlicher Port nach aussen geoeffnet.

Eigenschaften:
  * GET/HEAD mit absoluter URI (klassischer Proxy, genau was APT spricht)
  * CONNECT-Tunnel fuer https-Repositories
  * Host-Whitelist (Standard: nur Debian-Infrastruktur)
  * Platten-Cache fuer unveraenderliche Pfade (pool/, by-hash/, *.deb);
    Cache-Fehler beeinflussen die Uebertragung zum Client nie
  * /_health fuer den Tunnel-Check

Aufruf:
    python3 apt_proxy.py --port 3142 --cache ~/.cache/apt-via-ssh/pakete
"""

from __future__ import annotations

import argparse
import http.client
import logging
import os
import re
import select
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

# ---------------------------------------------------------------- Konstanten

# Diese Hosts sind ohne weitere Angabe erlaubt (Suffix-Vergleich).
STANDARD_HOSTS = [
    "debian.org",          # deckt deb./security./ftp.*./archive.debian.org ab
    "debian.net",
    "raspbian.org",
    "raspberrypi.org",
]

# Pfade, deren Inhalt sich nie aendert -> duerfen gecacht werden.
CACHEBAR = re.compile(r"(/pool/|/by-hash/|\.u?deb$|\.ddeb$|\.tar\.(gz|xz|bz2)$)")

# Header, die beim Weiterleiten nicht uebernommen werden.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
}

# Eigener Diagnose-Header. Absichtlich nicht "X-Cache": den setzen die
# CDNs vor den Debian-Mirrors (Fastly) bereits selbst.
CACHE_HEADER = "X-Apt-Via-Ssh"

PUFFER = 64 * 1024
PLATZ_RESERVE = 256 * 1024 * 1024   # so viel Platte bleibt immer frei

log = logging.getLogger("apt-proxy")


# ------------------------------------------------------------------- Zugriff

class Richtlinie:
    """Entscheidet, welche Hosts weitergeleitet werden duerfen."""

    def __init__(self, hosts: list[str], alles_erlauben: bool = False):
        self.alles_erlauben = alles_erlauben
        self.hosts = {h.strip().lstrip(".").lower() for h in hosts if h.strip()}

    def erlaubt(self, host: str) -> bool:
        if self.alles_erlauben:
            return True
        host = host.lower().split(":")[0].rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.hosts)


# --------------------------------------------------------------------- Cache

class CacheSchreiber:
    """
    Schreibt eine Antwort nebenbei in den Cache.

    Grundsatz: Der Client hat Vorrang. Jeder Fehler hier (Platte voll,
    Rechte, Pfad) deaktiviert nur den Schreiber und wird protokolliert --
    die Uebertragung zum Zielsystem laeuft unbeeindruckt weiter.
    """

    def __init__(self, ziel: str, erwartet: int | None, max_bytes: int):
        self.ziel = ziel
        self.erwartet = erwartet
        self.max_bytes = max_bytes
        self.geschrieben = 0
        self.temp: str | None = None
        self._fh = None
        try:
            os.makedirs(os.path.dirname(ziel), exist_ok=True)
            self.temp = f"{ziel}.{os.getpid()}.{threading.get_ident()}.part"
            self._fh = open(self.temp, "wb")
        except OSError as exc:
            log.warning("Cache aus (Anlegen fehlgeschlagen): %s", exc)
            self._aufgeben()

    @property
    def aktiv(self) -> bool:
        return self._fh is not None

    def schreiben(self, block: bytes) -> None:
        if not self._fh:
            return
        self.geschrieben += len(block)
        if self.max_bytes and self.geschrieben > self.max_bytes:
            log.info("Cache uebersprungen (> %d MiB): %s",
                     self.max_bytes // 1048576, os.path.basename(self.ziel))
            self._aufgeben()
            return
        try:
            self._fh.write(block)
        except OSError as exc:
            log.warning("Cache aus (Schreibfehler, Uebertragung laeuft weiter): %s", exc)
            self._aufgeben()

    def abschliessen(self) -> bool:
        """Benennt die fertige Datei um. Gibt True zurueck, wenn sie im Cache liegt."""
        if not self._fh:
            return False
        try:
            self._fh.close()
            self._fh = None
            if self.erwartet is not None and self.geschrieben != self.erwartet:
                log.warning("Cache aus (Laenge %d != erwartet %d): %s",
                            self.geschrieben, self.erwartet, self.ziel)
                self._aufgeben()
                return False
            os.replace(self.temp, self.ziel)
            self.temp = None
            return True
        except OSError as exc:
            log.warning("Cache aus (Abschluss fehlgeschlagen): %s", exc)
            self._aufgeben()
            return False

    def _aufgeben(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self.temp:
            try:
                os.unlink(self.temp)
            except OSError:
                pass
            self.temp = None


class Cache:
    """Schlichter Platten-Cache mit Pfadspiegelung (host/pfad)."""

    def __init__(self, verzeichnis: str | None, max_bytes: int = 0):
        self.basis = os.path.abspath(os.path.expanduser(verzeichnis)) if verzeichnis else None
        self.max_bytes = max_bytes
        if self.basis:
            try:
                os.makedirs(self.basis, exist_ok=True)
            except OSError as exc:
                log.warning("Cache deaktiviert (%s): %s", self.basis, exc)
                self.basis = None

    def aktiv(self) -> bool:
        return self.basis is not None

    def _pfad(self, host: str, pfad: str) -> str | None:
        if not self.basis:
            return None
        teile = [host.lower().replace(":", "_")]
        for t in pfad.split("?")[0].split("/"):
            if not t or t in (".", ".."):
                continue
            teile.append(re.sub(r"[^A-Za-z0-9._+~:@%-]", "_", t))
        if len(teile) < 2:
            return None
        ziel = os.path.join(self.basis, *teile)
        # Sicherheitsnetz gegen Ausbrueche aus dem Cache-Verzeichnis
        if not os.path.abspath(ziel).startswith(self.basis + os.sep):
            return None
        return ziel

    def holen(self, host: str, pfad: str) -> str | None:
        p = self._pfad(host, pfad)
        if p and os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
        return None

    def platz_reicht(self, erwartet: int | None) -> bool:
        if not self.basis:
            return False
        try:
            st = os.statvfs(self.basis)
        except OSError:
            return False
        frei = st.f_bavail * st.f_frsize
        return frei - (erwartet or 0) > PLATZ_RESERVE

    def schreiber(self, host: str, pfad: str, erwartet: int | None) -> CacheSchreiber | None:
        ziel = self._pfad(host, pfad)
        if not ziel:
            return None
        if self.max_bytes and erwartet is not None and erwartet > self.max_bytes:
            return None
        if not self.platz_reicht(erwartet):
            log.warning("Cache uebersprungen: zu wenig Platz in %s", self.basis)
            return None
        s = CacheSchreiber(ziel, erwartet, self.max_bytes)
        return s if s.aktiv else None


# ------------------------------------------------------------------- Handler

class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "apt-via-ssh"
    sys_version = ""

    # -- Infrastruktur ----------------------------------------------------
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        log.debug("%s %s", self.client_address[0], format % args)

    def _fehler(self, code: int, text: str) -> None:
        nutzlast = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(nutzlast)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(nutzlast)
        except OSError:
            pass
        self.close_connection = True

    # -- GET / HEAD -------------------------------------------------------
    def do_GET(self) -> None:
        self._weiterleiten("GET")

    def do_HEAD(self) -> None:
        self._weiterleiten("HEAD")

    def _weiterleiten(self, methode: str) -> None:
        if self.path in ("/_health", "/_health/"):
            nutzlast = b"apt-via-ssh proxy ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(nutzlast)))
            self.end_headers()
            if methode == "GET":
                self.wfile.write(nutzlast)
            return

        teile = urlsplit(self.path)
        if teile.scheme not in ("http", "https") or not teile.hostname:
            self._fehler(400, "Nur absolute http(s)-URIs (Proxy-Modus) werden unterstuetzt.")
            return

        host = teile.hostname
        if not self.server.richtlinie.erlaubt(host):
            log.warning("ABGELEHNT %s (nicht in der Whitelist)", host)
            self._fehler(403, f"Host nicht erlaubt: {host}")
            return

        pfad = teile.path + (("?" + teile.query) if teile.query else "")
        hat_range = any(k.lower() == "range" for k in self.headers)
        cachebar = (
            methode == "GET"
            and not hat_range
            and self.server.cache.aktiv()
            and bool(CACHEBAR.search(teile.path))
        )

        if cachebar:
            treffer = self.server.cache.holen(host, pfad)
            if treffer:
                self._aus_cache(treffer, host, pfad)
                return

        self._vom_netz(methode, teile, host, pfad, cachebar)

    def _aus_cache(self, datei: str, host: str, pfad: str) -> None:
        try:
            groesse = os.path.getsize(datei)
            self.send_response(200)
            self.send_header("Content-Length", str(groesse))
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(CACHE_HEADER, "HIT")
            self.end_headers()
            with open(datei, "rb") as fh:
                while block := fh.read(PUFFER):
                    self.wfile.write(block)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        except OSError as exc:
            log.warning("Cache-Datei unlesbar (%s): %s", datei, exc)
            self._fehler(502, "Cache-Datei unlesbar")
            return
        log.info("HIT  %s%s (%d B)", host, pfad, groesse)
        self.server.zaehler_erhoehen("treffer", groesse)

    def _vom_netz(self, methode: str, teile, host: str, pfad: str, cachebar: bool) -> None:
        port = teile.port or (443 if teile.scheme == "https" else 80)
        kopf = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() != "host"
        }
        kopf["Host"] = teile.netloc
        kopf["Connection"] = "close"

        verbindung = None
        try:
            if teile.scheme == "https":
                import ssl
                verbindung = http.client.HTTPSConnection(
                    host, port, timeout=self.server.zeitlimit,
                    context=ssl.create_default_context(),
                )
            else:
                verbindung = http.client.HTTPConnection(
                    host, port, timeout=self.server.zeitlimit)
            verbindung.request(methode, pfad or "/", headers=kopf)
            antwort = verbindung.getresponse()
        except Exception as exc:
            log.error("FEHLER %s%s: %s", host, pfad, exc)
            if verbindung:
                verbindung.close()
            self._fehler(502, f"Upstream-Fehler fuer {host}: {exc}")
            return

        schreiber = None
        try:
            self.send_response(antwort.status, antwort.reason)
            laenge = None
            for k, v in antwort.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                if k.lower() == "content-length":
                    try:
                        laenge = int(v)
                    except ValueError:
                        laenge = None
                self.send_header(k, v)
            self.send_header(CACHE_HEADER, "MISS")
            if laenge is None and methode != "HEAD":
                # Ohne Laengenangabe sauber mit Verbindungsabbau begrenzen.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            if methode == "HEAD":
                log.info("HEAD %s %s%s", antwort.status, host, pfad)
                return

            if cachebar and antwort.status == 200:
                schreiber = self.server.cache.schreiber(host, pfad, laenge)

            uebertragen = 0
            while block := antwort.read(PUFFER):
                # Zuerst der Client, danach der Cache -- in dieser Reihenfolge.
                self.wfile.write(block)
                uebertragen += len(block)
                if schreiber:
                    schreiber.schreiben(block)

            gecacht = schreiber.abschliessen() if schreiber else False
            log.info("MISS %s %s%s (%d B%s)", antwort.status, host, pfad,
                     uebertragen, ", gecacht" if gecacht else "")
            self.server.zaehler_erhoehen("verfehlt", uebertragen)
        except (BrokenPipeError, ConnectionResetError):
            log.info("ABBRUCH durch Client: %s%s", host, pfad)
            if schreiber:
                schreiber._aufgeben()
            self.close_connection = True
        finally:
            verbindung.close()

    # -- CONNECT (https) --------------------------------------------------
    def do_CONNECT(self) -> None:
        ziel = self.path.rsplit(":", 1)
        host = ziel[0].strip("[]")
        try:
            port = int(ziel[1]) if len(ziel) == 2 else 443
        except ValueError:
            self._fehler(400, "Ungueltiger CONNECT-Port.")
            return

        if not self.server.richtlinie.erlaubt(host):
            log.warning("ABGELEHNT CONNECT %s (nicht in der Whitelist)", host)
            self._fehler(403, f"Host nicht erlaubt: {host}")
            return
        if port not in self.server.connect_ports:
            self._fehler(403, f"CONNECT-Port nicht erlaubt: {port}")
            return

        try:
            gegenstelle = socket.create_connection((host, port), timeout=self.server.zeitlimit)
        except Exception as exc:
            log.error("FEHLER CONNECT %s:%s: %s", host, port, exc)
            self._fehler(502, f"Verbindung zu {host}:{port} fehlgeschlagen: {exc}")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()
        log.info("TUNNEL %s:%s", host, port)
        self.close_connection = True

        gegenstelle.setblocking(False)
        clientsocket = self.connection
        clientsocket.setblocking(False)
        bytes_total = 0
        try:
            while True:
                lesbar, _, fehlerhaft = select.select(
                    [clientsocket, gegenstelle], [], [clientsocket, gegenstelle], 60
                )
                if fehlerhaft or not lesbar:
                    break
                for quelle in lesbar:
                    senke = gegenstelle if quelle is clientsocket else clientsocket
                    try:
                        daten = quelle.recv(PUFFER)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    if not daten:
                        return
                    bytes_total += len(daten)
                    try:
                        senke.sendall(daten)
                    except OSError:
                        return
        finally:
            self.server.zaehler_erhoehen("tunnel", bytes_total)
            try:
                gegenstelle.close()
            except OSError:
                pass


# -------------------------------------------------------------------- Server

class ProxyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, adresse, richtlinie: Richtlinie, cache: Cache,
                 zeitlimit: float, connect_ports: set[int]):
        super().__init__(adresse, ProxyHandler)
        self.richtlinie = richtlinie
        self.cache = cache
        self.zeitlimit = zeitlimit
        self.connect_ports = connect_ports
        self._sperre = threading.Lock()
        self.statistik = {"treffer": 0, "verfehlt": 0, "tunnel": 0,
                          "bytes": 0, "start": time.time()}

    def zaehler_erhoehen(self, art: str, bytes_anzahl: int = 0) -> None:
        with self._sperre:
            self.statistik[art] = self.statistik.get(art, 0) + 1
            self.statistik["bytes"] += bytes_anzahl

    def handle_error(self, request, client_address) -> None:
        art = sys.exc_info()[0]
        if art in (BrokenPipeError, ConnectionResetError):
            return
        # Sichtbar protokollieren -- stille Ausnahmen kosten sonst Stunden.
        log.warning("Unerwarteter Fehler bei %s", client_address, exc_info=True)


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="HTTP-Proxy fuer APT hinter einem SSH-Reverse-Tunnel.")
    p.add_argument("--host", default="127.0.0.1",
                   help="Lausch-Adresse (Standard: 127.0.0.1 -- bitte so lassen)")
    p.add_argument("--port", type=int, default=3142, help="Lausch-Port (Standard: 3142)")
    p.add_argument("--cache", default="~/.cache/apt-via-ssh/pakete",
                   help="Cache-Verzeichnis; leerer Wert deaktiviert den Cache")
    p.add_argument("--cache-max-mb", type=int, default=1024,
                   help="Dateien groesser als dies nicht cachen (0 = unbegrenzt)")
    p.add_argument("--allow", action="append", default=[], metavar="HOST",
                   help="zusaetzlich erlaubter Host bzw. Domain-Suffix (mehrfach moeglich)")
    p.add_argument("--allow-all", action="store_true",
                   help="Whitelist abschalten (nur fuer Tests)")
    p.add_argument("--connect-port", action="append", type=int, default=[],
                   help="erlaubte CONNECT-Ports (Standard: 443)")
    p.add_argument("--timeout", type=float, default=30.0, help="Upstream-Zeitlimit in Sekunden")
    p.add_argument("--quiet", action="store_true", help="nur Warnungen und Fehler ausgeben")
    p.add_argument("--debug", action="store_true", help="ausfuehrliche Ausgabe")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    richtlinie = Richtlinie(STANDARD_HOSTS + args.allow, args.allow_all)
    cache = Cache(args.cache if args.cache.strip() else None,
                  max(0, args.cache_max_mb) * 1048576)
    ports = set(args.connect_port) or {443}

    try:
        server = ProxyServer((args.host, args.port), richtlinie, cache, args.timeout, ports)
    except OSError as exc:
        log.error("Kann nicht auf %s:%s lauschen: %s", args.host, args.port, exc)
        return 1

    log.info("Proxy laeuft auf %s:%s", args.host, args.port)
    log.info("Erlaubte Hosts: %s", "ALLE (unsicher)" if args.allow_all
             else ", ".join(sorted(richtlinie.hosts)))
    log.info("Cache: %s", cache.basis or "deaktiviert")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info("Beende ...")
    finally:
        s = server.statistik
        log.info("Bilanz: %d Cache-Treffer, %d Abrufe, %d Tunnel, %.1f MiB uebertragen",
                 s["treffer"], s["verfehlt"], s["tunnel"], s["bytes"] / 1048576)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
