"""
SSH-Sitzung zum Zielsystem fuer die apt-via-ssh-Oberflaeche.

Nutzt den OpenSSH-Client mit ControlMaster: Es gibt genau eine Anmeldung,
alle weiteren Befehle laufen ueber dieselbe Verbindung. Passwoerter gelangen
nur ueber SSH_ASKPASS an ssh (Datei mit Modus 0600 in einem 0700-Verzeichnis,
die direkt nach der Anmeldung geloescht wird) -- nie ueber die Befehlszeile.
"""

from __future__ import annotations

import os
import re
import selectors
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

HIER = Path(__file__).resolve().parent
ASKPASS = HIER / "askpass.sh"
KONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "apt-via-ssh"
KNOWN_HOSTS = KONFIG / "known_hosts"
HELFER = "/usr/local/sbin/apt-via-ssh-helper"

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%-]*$")
BENUTZER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}\$?$")
SU_PROMPT_RE = re.compile(r"(passwor[dt]|kennwort)[^:\n]*:\s*$", re.I)

Zeilenrueckruf = Callable[[str], None]


class SshFehler(Exception):
    """Fehler mit einer fuer Menschen lesbaren Meldung."""


def sq(s: str) -> str:
    return shlex.quote(s)


def _ohne_steuerzeichen(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s).replace("\r", "")


class Sitzung:
    ADMIN_ROLLEN = ("root", "sudo-ohne-pw", "sudo-pw", "su")

    def __init__(self, host: str, port: int = 22, benutzer: str = "root",
                 schluessel: str | None = None, zertifikat: str | None = None,
                 passwort: str | None = None, passphrase: str | None = None,
                 root_passwort: str | None = None):
        host = (host or "").strip()
        benutzer = (benutzer or "").strip()
        if not HOST_RE.match(host):
            raise SshFehler(f"Ungültige Zieladresse: {host!r}")
        if not BENUTZER_RE.match(benutzer):
            raise SshFehler(f"Ungültiger Benutzername: {benutzer!r}")
        if not 1 <= int(port) <= 65535:
            raise SshFehler("Ungültiger Port")
        for pfad in (schluessel, zertifikat):
            if pfad and not Path(os.path.expanduser(pfad)).is_file():
                raise SshFehler(f"Datei nicht gefunden: {pfad}")
        self.host = host
        self.port = int(port)
        self.benutzer = benutzer
        self.schluessel = os.path.expanduser(schluessel) if schluessel else None
        self.zertifikat = os.path.expanduser(zertifikat) if zertifikat else None
        self._passwort = passwort or None
        self._passphrase = passphrase or None
        self._root_passwort = root_passwort or None
        self.arbeit = tempfile.mkdtemp(prefix="apt-via-ssh-gui.")
        self.steuer = os.path.join(self.arbeit, "c")
        self.rolle = "keine"
        self.uid: int | None = None
        self.hat_sudo = False
        self.hat_helfer = False
        self.verbunden = False
        self.tunnel: tuple[int, int] | None = None   # (remote, lokal)

    # ------------------------------------------------------------ Grundlagen
    @property
    def host_spec(self) -> str:
        return self.host if self.port == 22 else f"[{self.host}]:{self.port}"

    @property
    def ist_admin(self) -> bool:
        return self.rolle in self.ADMIN_ROLLEN

    def beschreibung(self) -> str:
        return f"{self.benutzer}@{self.host}" + (f":{self.port}" if self.port != 22 else "")

    def _basis(self, master: bool = False) -> list[str]:
        a = ["ssh", "-p", str(self.port),
             "-o", f"ControlPath={self.steuer}",
             "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
             "-o", "StrictHostKeyChecking=yes",
             "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
             "-o", "ConnectTimeout=15",
             "-o", "LogLevel=ERROR",
             "-l", self.benutzer]
        if not master:
            a += ["-o", "ControlMaster=no", "-o", "BatchMode=yes"]
        if self.schluessel:
            a += ["-i", self.schluessel, "-o", "IdentitiesOnly=yes"]
        if self.zertifikat:
            a += ["-o", f"CertificateFile={self.zertifikat}"]
        return a

    # ------------------------------------------------------- Hostschluessel
    def hostkey_pruefen(self) -> dict:
        """Fragt die Hostschluessel ab und vergleicht sie mit known_hosts."""
        try:
            scan = subprocess.run(["ssh-keyscan", "-p", str(self.port), "-T", "8", self.host],
                                  capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise SshFehler(f"{self.host}:{self.port} antwortet nicht")
        zeilen = [z for z in scan.stdout.splitlines() if z and not z.startswith("#")]
        if not zeilen:
            raise SshFehler(f"Kein SSH-Dienst erreichbar unter {self.host}:{self.port}")
        fp = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(zeilen) + "\n",
                            capture_output=True, text=True)
        abdruecke = []
        for z in fp.stdout.splitlines():
            teile = z.split()
            if len(teile) >= 2:
                abdruecke.append({"typ": teile[-1].strip("()"), "fingerabdruck": teile[1]})
        bekannt = ""
        if KNOWN_HOSTS.exists():
            bekannt = subprocess.run(["ssh-keygen", "-F", self.host_spec, "-f", str(KNOWN_HOSTS)],
                                     capture_output=True, text=True).stdout
        bekannte_blobs = {z.split()[-1] for z in bekannt.splitlines() if z and not z.startswith("#")}
        neue_blobs = {z.split()[-1] for z in zeilen}
        if not bekannte_blobs:
            status = "neu"
        elif bekannte_blobs & neue_blobs:
            status = "bekannt"
        else:
            status = "geaendert"
        self._scan = zeilen
        return {"status": status, "schluessel": abdruecke}

    def hostkey_annehmen(self, ersetzen: bool = False) -> None:
        if not getattr(self, "_scan", None):
            self.hostkey_pruefen()
        KONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
        if ersetzen and KNOWN_HOSTS.exists():
            subprocess.run(["ssh-keygen", "-R", self.host_spec, "-f", str(KNOWN_HOSTS)],
                           capture_output=True)
            Path(str(KNOWN_HOSTS) + ".old").unlink(missing_ok=True)
        with open(KNOWN_HOSTS, "a", encoding="utf-8") as fh:
            for z in self._scan:
                fh.write(z + "\n")
        os.chmod(KNOWN_HOSTS, 0o600)

    # ------------------------------------------------------------ Verbinden
    def verbinden(self) -> None:
        geheim = os.path.join(self.arbeit, "g")
        os.mkdir(geheim, 0o700)
        try:
            for name, wert in (("passwort", self._passwort), ("passphrase", self._passphrase)):
                if wert:
                    fd = os.open(os.path.join(geheim, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as fh:
                        fh.write(wert)
            umgebung = dict(os.environ)
            umgebung.update(SSH_ASKPASS=str(ASKPASS), SSH_ASKPASS_REQUIRE="force",
                            APT_VIA_SSH_ASKPASS_DIR=geheim,
                            DISPLAY=os.environ.get("DISPLAY", ":0"))
            fehlerdatei = os.path.join(self.arbeit, "verbinden.err")
            cmd = self._basis(master=True) + [
                "-M", "-N", "-f", "-o", "ControlMaster=yes",
                "-o", "NumberOfPasswordPrompts=1", "-o", "ExitOnForwardFailure=yes",
                self.host]
            # stderr in eine Datei: der nach -f weiterlaufende Master hielte eine Pipe offen
            with open(fehlerdatei, "w") as err:
                try:
                    rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=err, env=umgebung, timeout=45).returncode
                except subprocess.TimeoutExpired:
                    raise SshFehler("Zeitüberschreitung bei der Anmeldung")
            if rc != 0:
                text = Path(fehlerdatei).read_text(errors="replace").strip()
                if "Permission denied" in text:
                    text = "Anmeldung abgelehnt (Benutzer, Passwort oder Schlüssel falsch)"
                elif "Host key verification failed" in text:
                    text = "Hostschlüssel nicht bestätigt"
                raise SshFehler(text or f"ssh endete mit Code {rc}")
        finally:
            shutil.rmtree(geheim, ignore_errors=True)
            self._passphrase = None
        self.verbunden = True
        self._rolle_ermitteln()

    def _rolle_ermitteln(self) -> None:
        rc, aus, _ = self.ausfuehren(
            "id -u; command -v sudo >/dev/null 2>&1 && echo sudo=ja || echo sudo=nein; "
            f"[ -x {HELFER} ] && echo helfer=ja || echo helfer=nein")
        z = aus.split()
        self.uid = int(z[0]) if z and z[0].isdigit() else None
        self.hat_sudo = "sudo=ja" in z
        self.hat_helfer = "helfer=ja" in z
        if self.uid == 0:
            self.rolle = "root"
            return
        if self.hat_sudo:
            voll = self.ausfuehren("sudo -n true", zeitlimit=15)[0] == 0
            if voll:
                self.rolle = "sudo-ohne-pw"
                return
            if self.hat_helfer and self.ausfuehren(f"sudo -n {HELFER} version", zeitlimit=15)[0] == 0:
                self.rolle = "wartung"
                return
            if self._passwort:
                rc, _, _ = self.ausfuehren("sudo -S -p '' -k true",
                                           eingabe=(self._passwort + "\n").encode(), zeitlimit=20)
                if rc == 0:
                    self.rolle = "sudo-pw"
                    return
        if self._root_passwort:
            rc, aus = self._su("id -u", None)
            if rc == 0 and aus.strip().splitlines()[-1:] == ["0"]:
                self.rolle = "su"
                return
        self.rolle = "keine"

    def root_passwort_setzen(self, pw: str) -> None:
        self._root_passwort = pw or None
        if self.verbunden:
            self._rolle_ermitteln()

    # -------------------------------------------------------- Ausfuehrung
    def ausfuehren(self, befehl: str, eingabe: bytes | None = None,
                   zeitlimit: float = 120) -> tuple[int, str, str]:
        if not self.verbunden:
            raise SshFehler("Nicht verbunden")
        cmd = self._basis() + ["-T", self.host, befehl]
        try:
            p = subprocess.run(cmd, input=eingabe, capture_output=True, timeout=zeitlimit,
                               stdin=None if eingabe is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise SshFehler(f"Zeitüberschreitung nach {zeitlimit:.0f} s")
        if p.returncode == 255 and not self.lebt():
            self.verbunden = False
            raise SshFehler("Verbindung zum Ziel verloren")
        return (p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace"))

    def strom(self, befehl: str, rueckruf: Zeilenrueckruf, eingabe: bytes | None = None,
              tty: bool = False, prompt_antwort: str | None = None,
              zeitlimit: float | None = None) -> int:
        """Fuehrt einen Befehl aus und meldet jede Ausgabezeile (stdout+stderr)."""
        if not self.verbunden:
            raise SshFehler("Nicht verbunden")
        cmd = self._basis() + (["-tt"] if tty else ["-T"]) + [self.host, befehl]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        if eingabe is not None:
            p.stdin.write(eingabe)
            p.stdin.flush()
        if prompt_antwort is None:
            p.stdin.close()
        sel = selectors.DefaultSelector()
        sel.register(p.stdout, selectors.EVENT_READ)
        puffer = b""
        anfang = time.monotonic()
        prompt_offen = prompt_antwort is not None
        try:
            while True:
                if zeitlimit and time.monotonic() - anfang > zeitlimit:
                    p.kill()
                    raise SshFehler(f"Zeitüberschreitung nach {zeitlimit:.0f} s")
                if prompt_offen and time.monotonic() - anfang > 20:
                    p.kill()
                    raise SshFehler("Keine Passwortabfrage von su erhalten")
                if not sel.select(timeout=0.5):
                    if p.poll() is not None:
                        break
                    continue
                block = os.read(p.stdout.fileno(), 65536)
                if not block:
                    break
                puffer += block
                if prompt_offen:
                    text = _ohne_steuerzeichen(puffer.decode(errors="replace"))
                    if SU_PROMPT_RE.search(text):
                        p.stdin.write((prompt_antwort + "\n").encode())
                        p.stdin.flush()
                        p.stdin.close()
                        prompt_offen = False
                        puffer = b""
                    continue
                *fertig, puffer = puffer.split(b"\n")
                for z in fertig:
                    rueckruf(_ohne_steuerzeichen(z.decode(errors="replace")))
            if puffer.strip() and not prompt_offen:
                rueckruf(_ohne_steuerzeichen(puffer.decode(errors="replace")))
            return p.wait()
        finally:
            sel.close()
            if p.poll() is None:
                p.kill()

    # --------------------------------------------------- mit Root-Rechten
    def _su(self, befehl: str, rueckruf: Zeilenrueckruf | None) -> tuple[int, str]:
        gesammelt: list[str] = []
        def sammeln(z: str) -> None:
            gesammelt.append(z)
            if rueckruf:
                rueckruf(z)
        rc = self.strom(f"su -s /bin/sh -c {sq(befehl)} root", sammeln, tty=True,
                        prompt_antwort=self._root_passwort or "", zeitlimit=None)
        return rc, "\n".join(gesammelt)

    def als_root(self, befehl: str, rueckruf: Zeilenrueckruf | None = None,
                 zeitlimit: float | None = 600) -> tuple[int, str]:
        """Fuehrt eine Shell-Befehlszeile mit Root-Rechten aus (Rolle bestimmt den Weg)."""
        if self.rolle == "root":
            vorne, eingabe = befehl, None
        elif self.rolle == "sudo-ohne-pw":
            vorne, eingabe = f"sudo -n sh -c {sq(befehl)}", None
        elif self.rolle == "wartung":
            # die sudoers-Regel erlaubt nur das Hilfsskript -> direkt aufrufen
            vorne, eingabe = f"sudo -n {befehl}", None
        elif self.rolle == "sudo-pw":
            vorne, eingabe = f"sudo -S -p '' -k sh -c {sq(befehl)}", (self._passwort + "\n").encode()
        elif self.rolle == "su":
            return self._su(befehl, rueckruf)
        else:
            raise SshFehler("Keine Root-Rechte auf dem Ziel (weder root, sudo noch su)")
        if rueckruf is None:
            rc, aus, err = self.ausfuehren(vorne, eingabe=eingabe, zeitlimit=zeitlimit or 600)
            return rc, aus + (("\n" + err) if err.strip() and rc != 0 else "")
        gesammelt: list[str] = []
        def sammeln(z: str) -> None:
            gesammelt.append(z)
            rueckruf(z)
        rc = self.strom(vorne, sammeln, eingabe=eingabe, zeitlimit=zeitlimit)
        return rc, "\n".join(gesammelt)

    def skript_als_root(self, skript: str, rueckruf: Zeilenrueckruf | None = None,
                        zeitlimit: float | None = 900) -> tuple[int, str]:
        """Uebertraegt ein Shell-Skript base64-kodiert und fuehrt es als root aus."""
        if self.rolle == "wartung":
            raise SshFehler("Der Wartungsbenutzer darf nur das Hilfsskript ausführen")
        import base64
        b64 = base64.b64encode(skript.encode()).decode()
        return self.als_root(f"bash -c 'eval \"$(printf %s {b64} | base64 -d)\"'", rueckruf, zeitlimit)

    def helfer(self, *argumente: str, rueckruf: Zeilenrueckruf | None = None,
               zeitlimit: float | None = 600) -> tuple[int, str]:
        befehl = " ".join(sq(a) for a in (HELFER, *argumente))
        return self.als_root(befehl, rueckruf, zeitlimit)

    # --------------------------------------------------------------- Tunnel
    def tunnel_auf(self, lokaler_port: int) -> int:
        if self.tunnel and self.tunnel[1] == lokaler_port:
            return self.tunnel[0]
        self.tunnel_zu()
        cmd = self._basis() + ["-O", "forward", "-R", f"0:127.0.0.1:{lokaler_port}", self.host]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        port = re.sub(r"\D", "", p.stdout)
        if p.returncode != 0 or not port:
            raise SshFehler("Reverse-Tunnel abgelehnt – Port-Weiterleitung für diesen Benutzer gesperrt"
                            + (f" ({p.stderr.strip()})" if p.stderr.strip() else ""))
        self.tunnel = (int(port), lokaler_port)
        return self.tunnel[0]

    def tunnel_zu(self) -> None:
        if self.tunnel and self.verbunden:
            r, l = self.tunnel
            subprocess.run(self._basis() + ["-O", "cancel", "-R", f"{r}:127.0.0.1:{l}", self.host],
                           capture_output=True, timeout=15)
        self.tunnel = None

    def tunnel_testen(self, lokaler_port: int) -> tuple[bool, str]:
        try:
            rport = self.tunnel_auf(lokaler_port)
        except SshFehler as e:
            return False, str(e)
        test = ("exec 3<>/dev/tcp/127.0.0.1/%d && printf 'GET /_health HTTP/1.0\\r\\n\\r\\n' >&3 "
                "&& cat <&3" % rport)
        rc, aus, err = self.ausfuehren(f"bash -c {sq(test)}", zeitlimit=20)
        if "apt-via-ssh proxy ok" in aus:
            return True, f"Ziel erreicht den Proxy unter 127.0.0.1:{rport}"
        return False, (err.strip() or aus.strip() or "keine Antwort über den Tunnel")

    # -------------------------------------------------------------- Ende
    def lebt(self) -> bool:
        if not os.path.exists(self.steuer):
            return False
        return subprocess.run(self._basis() + ["-O", "check", self.host],
                              capture_output=True, timeout=10).returncode == 0

    def trennen(self) -> None:
        try:
            if self.verbunden:
                self.tunnel_zu()
                subprocess.run(self._basis() + ["-O", "exit", self.host], capture_output=True, timeout=10)
        except Exception:
            pass
        finally:
            self.verbunden = False
            self._passwort = self._root_passwort = self._passphrase = None
            shutil.rmtree(self.arbeit, ignore_errors=True)
