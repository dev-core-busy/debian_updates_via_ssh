#!/usr/bin/env python3
"""
apt-via-ssh -- grafische Oberflaeche im Browser.

Startet einen kleinen Webserver NUR auf 127.0.0.1 (Python-Standardbibliothek).
Schutz: Host-Header-Pruefung (DNS-Rebinding), Sitzungs-Token im Cookie
UND im Header X-Token (CSRF), Content-Security-Policy ohne fremde Quellen.

    python3 gui/server.py [--port 8765] [--kein-browser]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
from sitzung import KONFIG, Sitzung, SshFehler  # noqa: E402

PROJEKT = HIER.parent
PROXY_SKRIPT = PROJEKT / "gateway" / "apt_proxy.py"
HELFER_DATEI = PROJEKT / "ziel" / "apt-via-ssh-helper"
EINRICHTEN = PROJEKT / "ziel" / "einrichten.sh"
ENTFERNEN = PROJEKT / "ziel" / "entfernen.sh"
STATIC = HIER / "static"
ZIELE_DATEI = KONFIG / "ziele.json"
SCHLUESSEL_VERZ = KONFIG / "schluessel"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "apt-via-ssh" / "pakete"
# Wartungsschluessel: alles verboten ausser Reverse-Tunnel auf localhost
SCHLUESSEL_OPT = 'restrict,port-forwarding,permitlisten="localhost:*"'
STANDARD_WARTUNG = "aptpflege"
PAKET_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*(:[a-z0-9-]+)?(=[A-Za-z0-9.+:~-]+)?$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")
ROLLEN_TEXT = {
    "root": "root", "sudo-ohne-pw": "sudo ohne Passwort", "sudo-pw": "sudo mit Passwort",
    "su": "su mit root-Passwort", "wartung": "Wartungsbenutzer (nur Paketverwaltung)",
    "keine": "keine Root-Rechte",
}


def helfer_version_lokal() -> str:
    m = re.search(r"^HELFER_VERSION=(\S+)", HELFER_DATEI.read_text(), re.M)
    return m.group(1) if m else "?"


def kv(text: str) -> dict:
    """key=value-Zeilen in ein dict."""
    d = {}
    for z in text.splitlines():
        if "=" in z:
            k, v = z.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def sim_auswerten(text: str) -> dict:
    """TSV-Ausgabe des Helfers (U/N/R/S/G/F/P/Q/I) auswerten."""
    e = {"aktualisiert": [], "neu": [], "entfernt": [], "zusammenfassung": "",
         "bytes": 0, "fehler": [], "upgrade_normal": set(), "suite": "", "info": []}
    for z in text.splitlines():
        t = z.split("\t")
        art = t[0]
        if art == "U" and len(t) >= 6:
            e["aktualisiert"].append({"name": t[1], "alt": t[2], "neu": t[3], "herkunft": t[4],
                                      "arch": t[5], "sicherheit": "security" in t[4].lower()})
        elif art == "N" and len(t) >= 6:
            e["neu"].append({"name": t[1], "neu": t[3], "herkunft": t[4], "arch": t[5]})
        elif art == "R" and len(t) >= 2:
            e["entfernt"].append({"name": t[1], "alt": t[2] if len(t) > 2 else ""})
        elif art == "S" and len(t) >= 2:
            e["zusammenfassung"] = t[1]
        elif art == "G" and len(t) >= 2 and t[1].isdigit():
            e["bytes"] = int(t[1])
        elif art == "F" and len(t) >= 2:
            e["fehler"].append(t[1])
        elif art == "P" and len(t) >= 2:
            e["upgrade_normal"].add(t[1])
        elif art == "Q" and len(t) >= 2:
            e["suite"] = t[1]
        elif art == "I" and len(t) >= 2:
            e["info"].append(t[1])
    for p in e["aktualisiert"]:
        p["zurueckgehalten"] = bool(e["upgrade_normal"]) and p["name"] not in e["upgrade_normal"]
    e["upgrade_normal"] = sorted(e["upgrade_normal"])
    return e


# ================================================================== Proxy
class ProxyDienst:
    def __init__(self, laufzeit: Path):
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.erlaubt: tuple[str, ...] = ()
        self.log = laufzeit / "proxy.log"
        self._sperre = threading.Lock()

    def sicherstellen(self, erlaubt: list[str]) -> int:
        with self._sperre:
            erlaubt_t = tuple(sorted(set(erlaubt)))
            if self.proc and self.proc.poll() is None and erlaubt_t == self.erlaubt:
                return self.port
            self._stop()
            s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
            cmd = [sys.executable, str(PROXY_SKRIPT), "--host", "127.0.0.1", "--port", str(port),
                   "--cache", str(CACHE)]
            for h in erlaubt_t:
                cmd += ["--allow", h]
            fh = open(self.log, "ab")
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=fh, stdin=subprocess.DEVNULL)
            fh.close()
            for _ in range(50):
                with socket.socket() as t:
                    t.settimeout(0.2)
                    if t.connect_ex(("127.0.0.1", port)) == 0:
                        break
                if self.proc.poll() is not None:
                    raise RuntimeError("Proxy konnte nicht gestartet werden")
                time.sleep(0.1)
            self.port, self.erlaubt = port, erlaubt_t
            return port

    def position(self) -> int:
        return self.log.stat().st_size if self.log.exists() else 0

    def gesperrt_seit(self, pos: int) -> list[str]:
        if not self.log.exists():
            return []
        with open(self.log, "rb") as fh:
            fh.seek(pos)
            text = fh.read().decode(errors="replace")
        return sorted(set(re.findall(r"ABGELEHNT (?:CONNECT )?(\S+)", text)))

    def _stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def stop(self) -> None:
        with self._sperre:
            self._stop()


# =============================================================== Auftraege
class Auftrag:
    def __init__(self, titel: str):
        self.id = uuid.uuid4().hex[:12]
        self.titel = titel
        self.zeilen: list[str] = []
        self.status = "laeuft"
        self.ergebnis = None
        self.fehler = ""
        self.start = time.time()
        self.ende: float | None = None
        self._sperre = threading.Lock()

    def log(self, text: str) -> None:
        with self._sperre:
            self.zeilen.extend(text.splitlines() or [""])

    def als_dict(self, ab: int = 0) -> dict:
        with self._sperre:
            return {"id": self.id, "titel": self.titel, "status": self.status,
                    "zeilen": self.zeilen[ab:], "anzahl": len(self.zeilen),
                    "ergebnis": self.ergebnis, "fehler": self.fehler,
                    "dauer": round((self.ende or time.time()) - self.start)}


class AuftragFehler(Exception):
    pass


# ==================================================================== App
class App:
    def __init__(self):
        self.laufzeit = Path(__import__("tempfile").mkdtemp(prefix="apt-via-ssh-gui-srv."))
        self.proxy = ProxyDienst(self.laufzeit)
        self.sitzung: Sitzung | None = None
        self.profil: dict | None = None
        self.hostkey_kandidat: Sitzung | None = None
        self.auftraege: dict[str, Auftrag] = {}
        self.aktiv: Auftrag | None = None
        self._sperre = threading.Lock()

    # ------------------------------------------------------------ Profile
    def ziele_laden(self) -> list[dict]:
        try:
            return json.loads(ZIELE_DATEI.read_text())
        except (OSError, ValueError):
            return []

    def ziele_speichern(self, ziele: list[dict]) -> None:
        KONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = ZIELE_DATEI.with_suffix(".tmp")
        tmp.write_text(json.dumps(ziele, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(ZIELE_DATEI)

    def profil_holen(self, host: str, port: int) -> dict:
        for z in self.ziele_laden():
            if z.get("host") == host and int(z.get("port", 22)) == int(port):
                return z
        return {"host": host, "port": int(port)}

    def profil_merken(self, profil: dict) -> None:
        ziele = [z for z in self.ziele_laden()
                 if not (z.get("host") == profil["host"] and int(z.get("port", 22)) == int(profil["port"]))]
        profil = {k: v for k, v in profil.items() if v not in (None, "")}
        ziele.insert(0, profil)
        self.ziele_speichern(ziele)
        self.profil = profil

    # ---------------------------------------------------------- Auftraege
    def auftrag_starten(self, titel: str, arbeit) -> Auftrag:
        with self._sperre:
            if self.aktiv and self.aktiv.status == "laeuft":
                raise AuftragFehler(f"Es läuft bereits: {self.aktiv.titel}")
            a = Auftrag(titel)
            self.auftraege[a.id] = a
            self.aktiv = a
        def lauf():
            try:
                a.ergebnis = arbeit(a)
                a.status = "ok"
            except Exception as exc:  # noqa: BLE001 -- jeder Fehler gehoert ins Protokoll
                a.status = "fehler"
                a.fehler = str(exc)
                a.log(f"FEHLER: {exc}")
            finally:
                a.ende = time.time()
        threading.Thread(target=lauf, daemon=True).start()
        return a

    # -------------------------------------------------------------- Hilfen
    def s(self) -> Sitzung:
        if not self.sitzung or not self.sitzung.verbunden:
            raise SshFehler("Nicht verbunden")
        return self.sitzung

    def proxy_port(self) -> int:
        erlaubt = (self.profil or {}).get("erlaubte_hosts", [])
        return self.proxy.sicherstellen(erlaubt)

    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.s().tunnel_auf(self.proxy_port())}"

    def helfer_pflicht(self) -> Sitzung:
        s = self.s()
        if s.rolle == "keine":
            raise SshFehler("Keine Root-Rechte – bitte root-Passwort (su) angeben oder als Wartungsbenutzer verbinden")
        rc, aus = s.helfer("version", zeitlimit=30)
        if rc != 0:
            raise SshFehler("Hilfsskript fehlt auf dem Ziel – bitte unter 'Voraussetzungen' einrichten")
        return s

    def zustand(self) -> dict:
        s = self.sitzung
        d = {"verbunden": bool(s and s.verbunden), "helfer_version": helfer_version_lokal(),
             "auftrag": self.aktiv.als_dict(len(self.aktiv.zeilen)) if self.aktiv else None,
             "profil": self.profil}
        if s and s.verbunden:
            d.update(ziel=s.host, port=s.port, benutzer=s.benutzer, rolle=s.rolle,
                     rolle_text=ROLLEN_TEXT.get(s.rolle, s.rolle), ist_admin=s.ist_admin,
                     beschreibung=s.beschreibung())
        return d

    def wartung_schluessel(self, host: str, port: int, benutzer: str) -> Path:
        SCHLUESSEL_VERZ.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}_{port}_{benutzer}") + "_ed25519"
        pfad = SCHLUESSEL_VERZ / name
        if not pfad.exists():
            kommentar = re.sub(r"\s", "_", f"apt-via-ssh_{benutzer}@{host}_von_{socket.gethostname()}")
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", kommentar, "-f", str(pfad)],
                           check=True, capture_output=True)
        return pfad

    def ziel_skript(self, vorlage: Path, werte: dict) -> str:
        kopf = "".join(f"{k}={shlex_quote(str(v))}\n" for k, v in werte.items())
        return kopf + vorlage.read_text()

    # ---------------------------------------------- Auftrag auf dem Ziel
    def verfolgen(self, a: Auftrag, s: Sitzung) -> dict:
        """Liest das Protokoll eines abgekoppelten Auftrags auf dem Ziel mit."""
        ab, rest, fehlversuche = 0, "", 0
        while True:
            try:
                rc, aus = s.helfer("auftrag", str(ab), zeitlimit=60)
                kopf, trenner, nutz = aus.partition("\n---\n")
                if rc != 0 or not trenner or "status=" not in kopf:
                    # z.B. waehrend sudo/bash/libc ausgetauscht werden: kurz keine gueltige Antwort
                    raise SshFehler(f"ungültige Antwort des Hilfsskripts (Exit-Code {rc})")
                fehlversuche = 0
            except SshFehler as exc:
                fehlversuche += 1
                if fehlversuche >= 24 or not s.verbunden:
                    raise SshFehler(f"{exc} – der Auftrag läuft auf dem Ziel weiter; "
                                    "nach dem Neuverbinden 'Laufenden Auftrag verfolgen'")
                a.log(f"(Abfrage gestört, neuer Versuch in 5 s: {exc})")
                time.sleep(5)
                continue
            k = kv(kopf)
            if "ende" in k and k["ende"].isdigit():
                ab = int(k["ende"])
            text = rest + nutz
            *zeilen, rest = text.split("\n")
            for z in zeilen:
                a.log(z)
            st = k.get("status", "keiner")
            if st != "laeuft":
                # einmal nachlesen: Zeilen, die zwischen Statusabfrage und Ende geschrieben wurden
                try:
                    _, aus2 = s.helfer("auftrag", str(ab), zeitlimit=60)
                    rest += aus2.partition("\n---\n")[2]
                except SshFehler:
                    pass
                for z in rest.split("\n")[:-1]:
                    a.log(z)
                rest = rest.split("\n")[-1]
                if rest:
                    a.log(rest)
                if st == "ok":
                    return {"status": st}
                if st == "keiner":
                    raise SshFehler("Auf dem Ziel ist kein Auftrag bekannt")
                raise SshFehler(f"Auftrag auf dem Ziel: {st} (Exit-Code {k.get('rc') or '?'})")
            time.sleep(1.5)


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


APP: App | None = None
TOKEN = secrets.token_urlsafe(24)
ERLAUBTE_HOSTS_HEADER: set[str] = set()


# ============================================================= Endpunkte
def api_zustand(_):
    return APP.zustand()


def api_ziele(_):
    return {"ziele": APP.ziele_laden()}


def api_ziel_loeschen(d):
    ziele = [z for z in APP.ziele_laden()
             if not (z.get("host") == d.get("host") and int(z.get("port", 22)) == int(d.get("port", 22)))]
    APP.ziele_speichern(ziele)
    return {"ziele": ziele}


def api_schluessel(_):
    ssh = Path.home() / ".ssh"
    schl, zert = [], []
    if ssh.is_dir():
        for f in sorted(ssh.iterdir()):
            if f.name.endswith("-cert.pub"):
                zert.append(str(f))
            elif f.suffix == "" and (ssh / (f.name + ".pub")).exists():
                schl.append(str(f))
    return {"schluessel": schl, "zertifikate": zert}


def _sitzung_aus(d: dict, modus: str) -> Sitzung:
    host = (d.get("host") or "").strip()
    port = int(d.get("port") or 22)
    if modus == "wartung":
        p = APP.profil_holen(host, port)
        if not p.get("wart_schluessel"):
            raise SshFehler("Für dieses Ziel ist noch kein Wartungszugang eingerichtet")
        return Sitzung(host, port, p.get("wart_benutzer", STANDARD_WARTUNG), schluessel=p["wart_schluessel"])
    return Sitzung(host, port, d.get("benutzer") or "root",
                   schluessel=d.get("schluessel") or None, zertifikat=d.get("zertifikat") or None,
                   passwort=d.get("passwort") or None, passphrase=d.get("passphrase") or None,
                   root_passwort=d.get("root_passwort") or None)


def api_hostkey(d):
    if APP.hostkey_kandidat:
        APP.hostkey_kandidat.trennen()
    s = Sitzung((d.get("host") or "").strip(), int(d.get("port") or 22), "root")
    APP.hostkey_kandidat = s
    return s.hostkey_pruefen()


def api_hostkey_annehmen(d):
    s = APP.hostkey_kandidat
    if not s or s.host != (d.get("host") or "").strip() or s.port != int(d.get("port") or 22):
        raise SshFehler("Bitte zuerst den Hostschlüssel abfragen")
    s.hostkey_annehmen(ersetzen=bool(d.get("ersetzen")))
    return {"ok": True}


def api_verbinden(d):
    if APP.aktiv and APP.aktiv.status == "laeuft":
        raise AuftragFehler("Während ein Auftrag läuft, kann nicht neu verbunden werden")
    modus = d.get("modus", "erstzugang")
    s = _sitzung_aus(d, modus)
    hk = s.hostkey_pruefen()
    if hk["status"] != "bekannt":
        # genau diesen Scan zum Annehmen vormerken (der Benutzer sieht diese Fingerabdruecke)
        if APP.hostkey_kandidat:
            APP.hostkey_kandidat.trennen()
        APP.hostkey_kandidat = s
        return {"hostkey": hk}
    s.verbinden()
    if APP.sitzung:
        APP.sitzung.trennen()
    APP.sitzung = s
    p = APP.profil_holen(s.host, s.port)
    if modus != "wartung":
        p.update(benutzer=s.benutzer, schluessel=d.get("schluessel") or "",
                 zertifikat=d.get("zertifikat") or "")
    p["zuletzt"] = time.strftime("%Y-%m-%d %H:%M")
    APP.profil_merken(p)
    return APP.zustand()


def api_trennen(_):
    if APP.sitzung:
        APP.sitzung.trennen()
    APP.sitzung = None
    return APP.zustand()


def api_root_passwort(d):
    APP.s().root_passwort_setzen(d.get("passwort") or "")
    return APP.zustand()


def api_voraussetzungen(_):
    s = APP.s()
    schritte = [{"schritt": "SSH-Anmeldung", "status": "OK", "text": f"angemeldet als {s.benutzer}"}]
    if s.rolle == "keine":
        schritte.append({"schritt": "Rechte", "status": "FEHLER",
                         "text": "keine Root-Rechte: root-Passwort für su angeben oder einen sudo-Benutzer verwenden"})
    else:
        schritte.append({"schritt": "Rechte", "status": "OK", "text": ROLLEN_TEXT[s.rolle]})
    ok, text = s.tunnel_testen(APP.proxy_port())
    schritte.append({"schritt": "Tunnel", "status": "OK" if ok else "FEHLER", "text": text})
    if s.ist_admin:
        p = APP.profil or {}
        pub = ""
        if p.get("wart_schluessel") and Path(p["wart_schluessel"] + ".pub").exists():
            pub = Path(p["wart_schluessel"] + ".pub").read_text().strip()
        werte = {"WART_BENUTZER": p.get("wart_benutzer", STANDARD_WARTUNG), "WART_SCHLUESSEL": pub,
                 "SCHLUESSEL_OPT": SCHLUESSEL_OPT, "HELFER_VERSION": helfer_version_lokal(),
                 "NUR_PRUEFEN": "1"}
        rc, aus = s.skript_als_root(APP.ziel_skript(EINRICHTEN, werte), zeitlimit=120)
        for z in aus.splitlines():
            m = re.match(r"^(OK|NEU|WARNUNG|FEHLER|INFO) ([^:]+): (.*)$", z)
            if m:
                schritte.append({"schritt": m.group(2), "status": m.group(1), "text": m.group(3)})
        if not pub:
            schritte.append({"schritt": "Wartungszugang", "status": "FEHLER",
                             "text": "noch nicht eingerichtet"})
    elif s.rolle == "wartung":
        rc, v = s.helfer("version", zeitlimit=30)
        v = v.strip()
        if v == helfer_version_lokal():
            schritte.append({"schritt": "Hilfsskript", "status": "OK", "text": f"Version {v}"})
        else:
            schritte.append({"schritt": "Hilfsskript", "status": "WARNUNG",
                             "text": f"Version {v or '?'} statt {helfer_version_lokal()} – mit Erstzugang neu einrichten"})
    fehler = [x for x in schritte if x["status"] == "FEHLER"]
    return {"schritte": schritte, "bereit": not fehler, "einrichtbar": s.ist_admin}


def api_einrichten(d):
    s = APP.s()
    if not s.ist_admin:
        raise SshFehler("Zum Einrichten wird ein Zugang mit Root-Rechten benötigt (Erstzugang)")
    weiterleitung = bool(d.get("weiterleitung", True))
    wart = (d.get("wart_benutzer") or (APP.profil or {}).get("wart_benutzer") or STANDARD_WARTUNG).strip()
    if not re.match(r"^[a-z_][a-z0-9_-]{0,30}$", wart):
        raise SshFehler("Ungültiger Name für den Wartungsbenutzer")

    def arbeit(a: Auftrag):
        a.log(f"Ziel: {s.beschreibung()} ({ROLLEN_TEXT[s.rolle]})")
        proxy = ""
        try:
            proxy = APP.proxy_url()
            a.log(f"Tunnel steht ({proxy}) – fehlende Pakete können nachinstalliert werden")
        except SshFehler as exc:
            a.log(f"Hinweis: kein Tunnel für den Erstzugang ({exc}); sudo kann ggf. nicht nachinstalliert werden")
        schl = APP.wartung_schluessel(s.host, s.port, wart)
        pub = Path(str(schl) + ".pub").read_text().strip()
        a.log(f"Wartungsschlüssel: {schl}")
        import base64
        werte = {"WART_BENUTZER": wart, "WART_SCHLUESSEL": pub, "SCHLUESSEL_OPT": SCHLUESSEL_OPT,
                 "HELFER_B64": base64.b64encode(HELFER_DATEI.read_bytes()).decode(),
                 "HELFER_VERSION": helfer_version_lokal(), "WEITERLEITUNG": "1" if weiterleitung else "0",
                 "PROXY": proxy, "NUR_PRUEFEN": "0"}
        a.log("--- Einrichtung auf dem Ziel ---")
        rc, aus = s.skript_als_root(APP.ziel_skript(EINRICHTEN, werte), rueckruf=a.log, zeitlimit=900)
        s.hat_helfer = True
        p = APP.profil_holen(s.host, s.port)
        p.update(wart_benutzer=wart, wart_schluessel=str(schl))
        APP.profil_merken(p)
        if rc != 0 and not proxy and re.search(r"^FEHLER sudo: ", aus, re.M):
            # Henne-Ei: sudo fehlt, der Erstzugang darf aber keinen Tunnel aufbauen.
            # Der Wartungsbenutzer darf es inzwischen -- sein Tunnel endet auf 127.0.0.1
            # des Ziels und ist damit auch fuer root nutzbar.
            a.log("--- 2. Durchgang: sudo über den Tunnel des Wartungsbenutzers installieren ---")
            t = Sitzung(s.host, s.port, wart, schluessel=str(schl))
            try:
                t.verbinden()
                werte["PROXY"] = f"http://127.0.0.1:{t.tunnel_auf(APP.proxy_port())}"
                a.log(f"Tunnel des Wartungsbenutzers: {werte['PROXY']}")
                rc, aus = s.skript_als_root(APP.ziel_skript(EINRICHTEN, werte), rueckruf=a.log, zeitlimit=900)
            finally:
                t.trennen()
        if rc != 0:
            raise SshFehler("Einrichtung mit Fehlern beendet (siehe Protokoll)")
        a.log("--- Test: Anmeldung als Wartungsbenutzer ---")
        t = Sitzung(s.host, s.port, wart, schluessel=str(schl))
        try:
            t.verbinden()
            a.log(f"Anmeldung als {wart}: ok, Rolle: {ROLLEN_TEXT.get(t.rolle, t.rolle)}")
            if t.rolle != "wartung":
                raise SshFehler(f"{wart} hat nicht die erwartete Rolle (statt dessen: {t.rolle})")
            ok, text = t.tunnel_testen(APP.proxy_port())
            a.log(("Tunnel als Wartungsbenutzer: ok – " if ok else "Tunnel als Wartungsbenutzer: FEHLER – ") + text)
            if not ok:
                raise SshFehler("Der Wartungsbenutzer kann keinen Tunnel aufbauen")
        finally:
            t.trennen()
        a.log("Wartungszugang ist einsatzbereit.")
        return {"wart_benutzer": wart}
    return APP.auftrag_starten(f"Wartungszugang '{wart}' einrichten", arbeit).als_dict()


def api_entfernen(_):
    s = APP.s()
    if not s.ist_admin:
        raise SshFehler("Zum Entfernen wird ein Zugang mit Root-Rechten benötigt")
    p = APP.profil_holen(s.host, s.port)
    wart = p.get("wart_benutzer", STANDARD_WARTUNG)

    def arbeit(a: Auftrag):
        rc, aus = s.skript_als_root(APP.ziel_skript(ENTFERNEN, {"WART_BENUTZER": wart}), rueckruf=a.log)
        if rc != 0:
            raise SshFehler("Entfernen mit Fehlern beendet")
        if p.get("wart_schluessel"):
            for f in (p["wart_schluessel"], p["wart_schluessel"] + ".pub"):
                Path(f).unlink(missing_ok=True)
        for k in ("wart_benutzer", "wart_schluessel"):
            p.pop(k, None)
        APP.profil_merken(p)
        s.hat_helfer = False
        return {}
    return APP.auftrag_starten(f"Wartungszugang '{wart}' entfernen", arbeit).als_dict()


def api_info(_):
    s = APP.helfer_pflicht()
    rc, aus = s.helfer("info", zeitlimit=60)
    if rc != 0:
        raise SshFehler(aus.strip() or "info fehlgeschlagen")
    return kv(aus)


def api_installiert(_):
    s = APP.helfer_pflicht()
    rc, aus = s.helfer("installiert", zeitlimit=120)
    pakete = []
    for z in aus.splitlines():
        t = z.split("\t")
        if len(t) >= 5:
            pakete.append({"name": t[0], "version": t[1], "arch": t[2],
                           "groesse_kb": int(t[3]) if t[3].isdigit() else 0, "beschreibung": t[4]})
    return {"pakete": pakete}


def _gesperrt_melden(a: Auftrag, pos: int) -> list[str]:
    g = APP.proxy.gesperrt_seit(pos)
    if g:
        a.log("Vom Proxy blockierte Repository-Hosts: " + ", ".join(g)
              + " – unter 'Repository-Hosts' freigeben")
    return g


def api_pruefen(_):
    s = APP.helfer_pflicht()

    def arbeit(a: Auftrag):
        url = APP.proxy_url()
        pos = APP.proxy.position()
        a.log(f"Paketlisten laden über {url} ...")
        rc, _ = s.helfer("update", url, rueckruf=a.log, zeitlimit=600)
        gesperrt = _gesperrt_melden(a, pos)
        if rc != 0:
            raise SshFehler("apt-get update fehlgeschlagen")
        rc, aus = s.helfer("aktualisierbar", zeitlimit=300)
        e = sim_auswerten(aus)
        e["gesperrt"] = gesperrt
        a.log(f"{len(e['aktualisiert'])} Aktualisierung(en) verfügbar"
              + (f", davon {sum(p['sicherheit'] for p in e['aktualisiert'])} Sicherheitsupdates"
                 if e["aktualisiert"] else ""))
        return e
    return APP.auftrag_starten("Auf Updates prüfen", arbeit).als_dict()


def _modus_pruefen(d: dict) -> tuple[str, list[str]]:
    modus = d.get("modus", "")
    pakete = [p for p in (d.get("pakete") or []) if p]
    if modus not in ("upgrade", "full-upgrade", "autoremove", "install", "only-upgrade"):
        raise SshFehler(f"Unbekannter Modus: {modus}")
    if modus in ("install", "only-upgrade"):
        if not pakete:
            raise SshFehler("Keine Pakete ausgewählt")
        for p in pakete:
            if not PAKET_RE.match(p):
                raise SshFehler(f"Ungültiger Paketname: {p}")
    else:
        pakete = []
    return modus, pakete


def api_simulieren(d):
    s = APP.helfer_pflicht()
    modus, pakete = _modus_pruefen(d)
    rc, aus = s.helfer("simulieren", modus, *pakete, zeitlimit=300)
    e = sim_auswerten(aus)
    if rc != 0 and not e["fehler"]:
        e["fehler"].append(aus.strip().splitlines()[-1] if aus.strip() else f"Exit-Code {rc}")
    e["ok"] = rc == 0
    return e


def api_ausfuehren(d):
    s = APP.helfer_pflicht()
    modus, pakete = _modus_pruefen(d)
    titel = {"upgrade": "Updates einspielen", "full-upgrade": "Alle Updates einspielen (full-upgrade)",
             "autoremove": "Nicht mehr benötigte Pakete entfernen", "install": "Pakete installieren",
             "only-upgrade": "Ausgewählte Updates einspielen"}[modus]

    def arbeit(a: Auftrag):
        if modus != "autoremove":
            url = APP.proxy_url()
            pos = APP.proxy.position()
            a.log("--- Herunterladen über den Tunnel ---")
            rc, _ = s.helfer("laden", url, modus, *pakete, rueckruf=a.log, zeitlimit=3600)
            _gesperrt_melden(a, pos)
            if rc != 0:
                raise SshFehler("Herunterladen fehlgeschlagen – es wurde nichts installiert")
        a.log("--- Installation auf dem Ziel (läuft unabhängig von der SSH-Verbindung) ---")
        rc, aus = s.helfer("starten", modus, *pakete, zeitlimit=60)
        if rc != 0:
            raise SshFehler(aus.strip() or "Start fehlgeschlagen")
        return APP.verfolgen(a, s)
    return APP.auftrag_starten(titel, arbeit).als_dict()


def api_verfolgen(_):
    s = APP.helfer_pflicht()
    return APP.auftrag_starten("Laufenden Auftrag auf dem Ziel verfolgen",
                               lambda a: APP.verfolgen(a, s)).als_dict()


def api_dist(_):
    s = APP.helfer_pflicht()
    rc, aus = s.helfer("dist-info", zeitlimit=60)
    if rc != 0:
        return {"moeglich": False, "grund": aus.strip().replace("FEHLER: ", "")}
    d = {"moeglich": True, "quellen_neu": [], "quellen_gleich": []}
    for z in aus.splitlines():
        k, _, v = z.partition("=")
        if k in ("quelle_neu", "quelle_gleich"):
            d["quellen_neu" if k == "quelle_neu" else "quellen_gleich"].append(v)
        elif k:
            d[k] = v
    return d


def api_dist_simulieren(_):
    s = APP.helfer_pflicht()

    def arbeit(a: Auftrag):
        url = APP.proxy_url()
        pos = APP.proxy.position()
        alles: list[str] = []
        def mit(z: str):
            alles.append(z)
            if z.startswith(("I\t", "F\t")):
                a.log(z.split("\t", 1)[1])
        a.log("Lade die Paketlisten des neuen Release in ein Testverzeichnis (am System ändert sich nichts) ...")
        rc, _ = s.helfer("dist-simulieren", url, rueckruf=mit, zeitlimit=1200)
        e = sim_auswerten("\n".join(alles))
        e["gesperrt"] = _gesperrt_melden(a, pos)
        if rc != 0:
            raise SshFehler("Simulation fehlgeschlagen")
        a.log(f"Ergebnis: {len(e['aktualisiert'])} aktualisiert, {len(e['neu'])} neu, "
              f"{len(e['entfernt'])} entfernt, Download {e['bytes'] / 1048576:.0f} MiB")
        if e["suite"]:
            a.log(f"Das neue Release ist derzeit: {e['suite']}")
        return e
    return APP.auftrag_starten("Distributions-Upgrade simulieren", arbeit).als_dict()


def api_dist_ausfuehren(d):
    s = APP.helfer_pflicht()
    if d.get("bestaetigung") != "UPGRADE":
        raise SshFehler("Bitte zur Bestätigung UPGRADE eingeben")

    def zurueck(a: Auftrag, url: str):
        a.log("--- Stelle die alten Paketquellen wieder her ---")
        s.helfer("dist-zuruecksetzen", rueckruf=a.log, zeitlimit=60)
        s.helfer("update", url, rueckruf=a.log, zeitlimit=600)

    def arbeit(a: Auftrag):
        url = APP.proxy_url()
        a.log("--- 1/4 Paketquellen umstellen (Sicherung wird angelegt) ---")
        rc, _ = s.helfer("dist-vorbereiten", rueckruf=a.log, zeitlimit=60)
        if rc != 0:
            raise SshFehler("Umstellen der Paketquellen fehlgeschlagen")
        a.log("--- 2/4 Neue Paketlisten laden (jeder Ladefehler bricht ab) ---")
        pos = APP.proxy.position()
        rc, _ = s.helfer("update-streng", url, rueckruf=a.log, zeitlimit=900)
        _gesperrt_melden(a, pos)
        if rc != 0:
            zurueck(a, url)
            raise SshFehler("Paketlisten des neuen Release nicht ladbar – Quellen zurückgesetzt")
        a.log("--- 3/4 Alle Pakete herunterladen ---")
        rc, _ = s.helfer("laden", url, "full-upgrade", rueckruf=a.log, zeitlimit=7200)
        if rc != 0:
            zurueck(a, url)
            raise SshFehler("Herunterladen fehlgeschlagen – Quellen zurückgesetzt, nichts installiert")
        a.log("--- 4/4 Installation (läuft unabhängig von der SSH-Verbindung) ---")
        rc, aus = s.helfer("dist-starten", zeitlimit=60)
        if rc != 0:
            raise SshFehler(aus.strip() or "Start fehlgeschlagen")
        APP.verfolgen(a, s)
        rc, info = s.helfer("info", zeitlimit=60)
        i = kv(info)
        a.log(f"Jetzt: {i.get('os', '?')}. Neustart erforderlich: {i.get('neustart', '?')}")
        return {"info": i}
    return APP.auftrag_starten("Distributions-Upgrade", arbeit).als_dict()


def api_dist_zuruecksetzen(_):
    s = APP.helfer_pflicht()
    rc, aus = s.helfer("dist-zuruecksetzen", zeitlimit=60)
    if rc != 0:
        raise SshFehler(aus.strip())
    return {"text": aus.strip()}


def api_quelle_deaktivieren(d):
    s = APP.helfer_pflicht()
    datei = os.path.basename(d.get("datei") or "")
    rc, aus = s.helfer("quelle-deaktivieren", datei, zeitlimit=30)
    if rc != 0:
        raise SshFehler(aus.strip())
    return {"text": aus.strip()}


def api_quellen(_):
    s = APP.helfer_pflicht()
    rc, aus = s.helfer("quellen", zeitlimit=30)
    return {"text": aus}


def api_neustart(_):
    s = APP.helfer_pflicht()
    rc, aus = s.helfer("neustart", zeitlimit=30)
    if rc != 0:
        raise SshFehler(aus.strip())
    return {"text": aus.strip()}


def api_erlauben(d):
    host = (d.get("host") or "").strip().lower()
    if not HOSTNAME_RE.match(host):
        raise SshFehler("Ungültiger Hostname")
    s = APP.s()
    p = APP.profil_holen(s.host, s.port)
    hosts = set(p.get("erlaubte_hosts", []))
    if d.get("entfernen"):
        hosts.discard(host)
    else:
        hosts.add(host)
    p["erlaubte_hosts"] = sorted(hosts)
    APP.profil_merken(p)
    return {"erlaubte_hosts": p["erlaubte_hosts"]}


def api_auftrag(_, q):
    a = APP.auftraege.get((q.get("id") or [""])[0])
    if not a:
        raise SshFehler("Auftrag unbekannt")
    ab = int((q.get("ab") or ["0"])[0] or 0)
    return a.als_dict(ab)


GET_ROUTEN = {"/api/zustand": api_zustand, "/api/ziele": api_ziele, "/api/schluessel": api_schluessel,
              "/api/voraussetzungen": api_voraussetzungen, "/api/info": api_info,
              "/api/installiert": api_installiert, "/api/dist": api_dist, "/api/quellen": api_quellen}
POST_ROUTEN = {"/api/ziel-loeschen": api_ziel_loeschen, "/api/hostkey": api_hostkey,
               "/api/hostkey-annehmen": api_hostkey_annehmen, "/api/verbinden": api_verbinden,
               "/api/trennen": api_trennen, "/api/root-passwort": api_root_passwort,
               "/api/einrichten": api_einrichten, "/api/entfernen": api_entfernen,
               "/api/pruefen": api_pruefen, "/api/simulieren": api_simulieren,
               "/api/ausfuehren": api_ausfuehren, "/api/verfolgen": api_verfolgen,
               "/api/dist-simulieren": api_dist_simulieren, "/api/dist-ausfuehren": api_dist_ausfuehren,
               "/api/dist-zuruecksetzen": api_dist_zuruecksetzen,
               "/api/quelle-deaktivieren": api_quelle_deaktivieren,
               "/api/neustart": api_neustart, "/api/erlauben": api_erlauben}
STATISCH = {"/app.js": "application/javascript; charset=utf-8", "/style.css": "text/css; charset=utf-8"}


# ================================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "apt-via-ssh-gui"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: A002
        if os.environ.get("APT_VIA_SSH_GUI_DEBUG"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _kopf_sicherheit(self):
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _senden(self, code: int, daten: bytes, typ: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(daten)))
        self._kopf_sicherheit()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(daten)

    def _json(self, code: int, obj):
        self._senden(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in ERLAUBTE_HOSTS_HEADER

    def _cookie_ok(self) -> bool:
        for teil in self.headers.get("Cookie", "").split(";"):
            k, _, v = teil.strip().partition("=")
            if k == "avs" and secrets.compare_digest(v, TOKEN):
                return True
        return False

    def _api_ok(self) -> bool:
        return self._cookie_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        if not self._host_ok():
            return self._senden(421, b"Falscher Host", "text/plain; charset=utf-8")
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path == "/" and q.get("t", [""])[0]:
            if secrets.compare_digest(q["t"][0], TOKEN):
                return self._senden(303, b"", "text/plain", {
                    "Location": "/", "Set-Cookie": f"avs={TOKEN}; HttpOnly; SameSite=Strict; Path=/"})
            return self._senden(403, "Ungültiger Link".encode(), "text/plain; charset=utf-8")
        if not self._cookie_ok():
            return self._senden(403, "Bitte die Oberfläche über den Link öffnen, den apt-via-ssh-gui "
                                     "beim Start ausgibt.".encode(), "text/plain; charset=utf-8")
        if url.path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text().replace("%%TOKEN%%", TOKEN)
            return self._senden(200, html.encode(), "text/html; charset=utf-8")
        if url.path in STATISCH:
            return self._senden(200, (STATIC / url.path.lstrip("/")).read_bytes(), STATISCH[url.path])
        if url.path == "/api/auftrag" and self._api_ok():
            return self._ausfuehren(lambda: api_auftrag(None, q))
        if url.path in GET_ROUTEN and self._api_ok():
            return self._ausfuehren(lambda: GET_ROUTEN[url.path](None))
        self._senden(404, b"nicht gefunden", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return self._senden(421, b"Falscher Host", "text/plain; charset=utf-8")
        if not self._api_ok():
            return self._json(403, {"fehler": "Nicht angemeldet (Token)"})
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self._json(415, {"fehler": "JSON erwartet"})
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge > 1_000_000:
            return self._json(413, {"fehler": "zu groß"})
        try:
            daten = json.loads(self.rfile.read(laenge) or b"{}")
        except ValueError:
            return self._json(400, {"fehler": "ungültiges JSON"})
        f = POST_ROUTEN.get(urlsplit(self.path).path)
        if not f:
            return self._json(404, {"fehler": "unbekannt"})
        self._ausfuehren(lambda: f(daten))

    def _ausfuehren(self, f):
        try:
            self._json(200, f())
        except AuftragFehler as exc:
            self._json(409, {"fehler": str(exc)})
        except SshFehler as exc:
            self._json(400, {"fehler": str(exc)})
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._json(500, {"fehler": f"Interner Fehler: {exc}"})


def main() -> int:
    global APP
    ap = argparse.ArgumentParser(description="apt-via-ssh: Oberfläche im Browser")
    ap.add_argument("--port", type=int, default=8765, help="Port auf 127.0.0.1 (Vorgabe 8765, belegt -> frei)")
    ap.add_argument("--kein-browser", action="store_true", help="Browser nicht automatisch öffnen")
    args = ap.parse_args()

    APP = App()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    ERLAUBTE_HOSTS_HEADER.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    url = f"http://127.0.0.1:{port}/?t={TOKEN}"
    print(f"apt-via-ssh Oberfläche: {url}", flush=True)
    print("(Nur auf diesem Rechner erreichbar. Beenden mit Strg-C.)", flush=True)
    if not args.kein_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    def ende(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, ende)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if APP.sitzung:
            APP.sitzung.trennen()
        if APP.hostkey_kandidat:
            APP.hostkey_kandidat.trennen()
        APP.proxy.stop()
        shutil.rmtree(APP.laufzeit, ignore_errors=True)
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
