#!/usr/bin/env python3
"""
End-to-End-Test der Oberflaeche (ueber ihre HTTP-API) gegen zwei abgeschottete
Debian-12-Container. Dauert wegen des echten Distributions-Upgrades einige Minuten.

    python3 tests/gui-test.py [--ohne-dist]

Benoetigt Docker. Konfiguration, Schluessel und known_hosts landen in einem
Temp-Verzeichnis -- ~/.config bleibt unberuehrt.
"""
import argparse, http.client, json, os, re, subprocess, sys, tempfile, time, shutil
from pathlib import Path

HIER = Path(__file__).resolve().parent
PROJEKT = HIER.parent
NETZ = "apt-via-ssh-gui-iso"
TAG = "bookworm-20240513"
PW_VERWALTER, PW_ROOT = "Test-Verwalter-4711", "Test-Root-4711"   # Testwerte aus Dockerfile.gui
ok = fehl = 0


def gut(t):
    global ok; ok += 1; print(f"  \033[32mOK\033[0m    {t}", flush=True)


def schlecht(t):
    global fehl; fehl += 1; print(f"  \033[31mFEHL\033[0m  {t}", flush=True)


def pruefe(bed, t):
    gut(t) if bed else schlecht(t)
    return bed


def abschnitt(t):
    print(f"\n== {t} ==", flush=True)


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)


def dexec(c, cmd):
    return sh("docker", "exec", c, "sh", "-c", cmd).stdout.strip()


class Gui:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, str(PROJEKT / "gui" / "server.py"), "--kein-browser", "--port", "0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        zeile = self.p.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=(\S+)", zeile)
        assert m, zeile
        self.port, self.token = int(m.group(1)), m.group(2)
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", f"/?t={self.token}")
        r = c.getresponse(); r.read()
        self.cookie = r.getheader("Set-Cookie").split(";")[0]

    def api(self, pfad, daten=None, erwarte=200):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=900)
        k = {"X-Token": self.token, "Cookie": self.cookie, "Host": f"127.0.0.1:{self.port}"}
        if daten is None:
            c.request("GET", pfad, headers=k)
        else:
            k["Content-Type"] = "application/json"
            c.request("POST", pfad, body=json.dumps(daten), headers=k)
        r = c.getresponse()
        j = json.loads(r.read() or b"{}")
        if erwarte and r.status != erwarte:
            raise AssertionError(f"{pfad}: HTTP {r.status} {j}")
        return j

    def folgen(self, a, zeigen=False):
        ab = 0
        while True:
            r = self.api(f"/api/auftrag?id={a['id']}&ab={ab}")
            for z in r["zeilen"]:
                if zeigen or re.search(r"FEHLER|Fehler|^E:|>>> ", z):
                    print("        | " + z[:150], flush=True)
            ab = r["anzahl"]
            if r["status"] != "laeuft":
                return r
            time.sleep(1)

    def verbinden(self, d):
        r = self.api("/api/verbinden", d)
        if "hostkey" in r:
            self.api("/api/hostkey-annehmen", {"host": d["host"], "port": 22})
            r = self.api("/api/verbinden", d)
        return r

    def ende(self):
        self.p.terminate()
        try:
            self.p.wait(10)
        except subprocess.TimeoutExpired:
            self.p.kill()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--ohne-dist", action="store_true")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="apt-via-ssh-guitest."))
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp / "config"), XDG_CACHE_HOME=str(tmp / "cache"))
    container = {}
    gui = None
    try:
        abschnitt("Vorbereitung")
        for v in ("sudo", "su"):
            r = sh("docker", "build", "-q", "--build-arg", f"DEBIAN_TAG={TAG}", "--build-arg", f"VARIANTE={v}",
                   "-t", f"apt-via-ssh-gui-ziel:{v}", "-f", str(HIER / "docker" / "Dockerfile.gui"), str(HIER / "docker"))
            if r.returncode:
                print(r.stderr); return 2
        sh("docker", "rm", "-f", "avs-gui-sudo", "avs-gui-su"); sh("docker", "network", "rm", NETZ)
        sh("docker", "network", "create", "--internal", NETZ)
        for v in ("sudo", "su"):
            n = f"avs-gui-{v}"
            sh("docker", "run", "-d", "--name", n, "--network", NETZ, f"apt-via-ssh-gui-ziel:{v}")
            ip = sh("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", n).stdout.strip()
            container[v] = (n, ip)
            print(f"  {n}: {ip}")
        time.sleep(2)
        pruefe(dexec(container["sudo"][0], "timeout 5 bash -c 'exec 3<>/dev/tcp/151.101.2.132/80' 2>/dev/null && echo offen || echo zu").endswith("zu"),
               "Container haben kein Internet")
        gui = Gui(env)
        gut(f"Oberflaeche laeuft auf 127.0.0.1:{gui.port}")

        # Sicherheit des lokalen Servers
        c = http.client.HTTPConnection("127.0.0.1", gui.port); c.request("GET", "/api/zustand"); r = c.getresponse(); r.read()
        pruefe(r.status == 404 or r.status == 403, f"API ohne Token abgewiesen ({r.status})")
        c = http.client.HTTPConnection("127.0.0.1", gui.port)
        c.request("POST", "/api/trennen", body="{}", headers={"Cookie": gui.cookie, "Content-Type": "application/json"})
        r = c.getresponse(); r.read()
        pruefe(r.status == 403, "POST mit Cookie, aber ohne X-Token abgewiesen (CSRF)")
        c = http.client.HTTPConnection("127.0.0.1", gui.port)
        c.request("GET", "/", headers={"Host": "boese.example:80", "Cookie": gui.cookie}); r = c.getresponse(); r.read()
        pruefe(r.status == 421, "fremder Host-Header abgewiesen (DNS-Rebinding)")

        # ------------------------------------------------------- Ziel A: sudo
        n, ip = container["sudo"]
        abschnitt(f"Ziel A ({ip}): sudo mit Passwort")
        try:
            gui.api("/api/verbinden", {"host": ip, "benutzer": "verwalter", "passwort": "falsch"}, erwarte=None)
            r = gui.api("/api/hostkey", {"host": ip, "port": 22})
            pruefe(r["status"] == "neu" and r["schluessel"], f"Hostschluessel neu, {len(r['schluessel'])} Fingerabdruecke")
            gui.api("/api/hostkey-annehmen", {"host": ip, "port": 22})
            r = gui.api("/api/verbinden", {"host": ip, "benutzer": "verwalter", "passwort": "falsch"}, erwarte=400)
            pruefe("abgelehnt" in r.get("fehler", ""), "falsches Passwort -> verstaendliche Meldung")
        except AssertionError as e:
            schlecht(str(e))
        z = gui.verbinden({"host": ip, "benutzer": "verwalter", "passwort": PW_VERWALTER})
        pruefe(z.get("rolle") == "sudo-pw", f"Erstzugang: Rolle {z.get('rolle')}")
        v = gui.api("/api/voraussetzungen")
        st = {s["schritt"]: s["status"] for s in v["schritte"]}
        pruefe(st.get("Tunnel") == "OK" and st.get("Hilfsskript") == "FEHLER" and v["einrichtbar"],
               f"Voraussetzungen vor dem Einrichten: Tunnel {st.get('Tunnel')}, Hilfsskript {st.get('Hilfsskript')}")
        r = gui.folgen(gui.api("/api/einrichten", {"wart_benutzer": "aptpflege", "weiterleitung": True}))
        pruefe(r["status"] == "ok", "Wartungszugang eingerichtet" + ("" if r["status"] == "ok" else f": {r['fehler']}"))
        pruefe(dexec(n, "sudo -l -U aptpflege | grep -c apt-via-ssh-helper") == "1", "sudo-Regel erlaubt nur das Hilfsskript")
        pruefe("restrict" in dexec(n, "cat /home/aptpflege/.ssh/authorized_keys"), "Schluessel mit restrict-Optionen")
        fw = dexec(n, "sshd -T -C user=aptpflege,host=x,addr=127.0.0.1 | grep -iE '^(allowtcpforwarding|permitopen) '")
        pruefe(sorted(fw.split()) == sorted(["allowtcpforwarding", "remote", "permitopen", "none"]),
               f"auch bei global offener Weiterleitung: aptpflege nur Reverse-Tunnel ({' / '.join(fw.splitlines())})")
        z = gui.verbinden({"host": ip, "modus": "wartung"})
        pruefe(z.get("rolle") == "wartung", f"als Wartungsbenutzer verbunden ({z.get('rolle')})")
        v = gui.api("/api/voraussetzungen")
        pruefe(v["bereit"], "Voraussetzungen erfuellt: " + ", ".join(f"{s['schritt']}={s['status']}" for s in v["schritte"]))
        i = gui.api("/api/info")
        pruefe(i.get("codename") == "bookworm", f"Info: {i.get('os')}, {i.get('pakete')} Pakete")
        inst = gui.api("/api/installiert")["pakete"]
        pruefe(len(inst) > 50 and any(p["name"] == "openssh-server" for p in inst), f"Installierte Pakete: {len(inst)}")
        r = gui.folgen(gui.api("/api/pruefen", {}))
        upd = r["ergebnis"]["aktualisiert"] if r["status"] == "ok" else []
        sich = sum(p["sicherheit"] for p in upd)
        pruefe(len(upd) > 10, f"Auf Updates geprueft: {len(upd)} verfuegbar, {sich} Sicherheitsupdates")
        drei = [p["name"] for p in upd if not p["zurueckgehalten"]][:3]
        s = gui.api("/api/simulieren", {"modus": "only-upgrade", "pakete": drei})
        pruefe(s["ok"] and len(s["aktualisiert"]) >= 3, f"Simulation fuer {drei}: {len(s['aktualisiert'])} aktualisiert, {s['bytes']} B")
        r = gui.folgen(gui.api("/api/ausfuehren", {"modus": "only-upgrade", "pakete": drei}))
        pruefe(any(x.startswith("Setting up ") for x in r.get("zeilen", [])) or
               any("Setting up " in x for x in gui.api(f"/api/auftrag?id={r['id']}&ab=0")["zeilen"]),
               "Protokoll der abgekoppelten Installation vollstaendig mitgelesen (dpkg 'Setting up')")
        neu = {p["name"]: p["neu"] for p in upd}
        ist = {d: dexec(n, f"dpkg-query -W -f='${{Version}}' {d}") for d in drei}
        pruefe(r["status"] == "ok" and all(ist[d] == neu[d] for d in drei), f"3 ausgewaehlte Updates eingespielt: {ist}")
        s = gui.api("/api/simulieren", {"modus": "install", "pakete": ["gibtsnicht-xyz"]})
        pruefe(not s["ok"] and s["fehler"], f"unbekanntes Paket -> Fehler in der Vorschau: {s['fehler'][:1]}")
        r = gui.api("/api/simulieren", {"modus": "install", "pakete": ["-o", "x"]}, erwarte=400)
        pruefe("Ungültiger Paketname" in r.get("fehler", ""), "Options-Injektion ueber Paketnamen abgewiesen")
        r = gui.folgen(gui.api("/api/ausfuehren", {"modus": "install", "pakete": ["sl"]}))
        pruefe(r["status"] == "ok" and dexec(n, "dpkg -s sl >/dev/null && echo ja") == "ja", "Paket sl installiert")
        r = gui.folgen(gui.api("/api/ausfuehren", {"modus": "full-upgrade", "pakete": []}))
        pruefe(r["status"] == "ok", "alle uebrigen Updates eingespielt (full-upgrade)")
        r = gui.folgen(gui.api("/api/pruefen", {}))
        pruefe(r["status"] == "ok" and not r["ergebnis"]["aktualisiert"], "danach: keine Updates mehr offen")

        if not args.ohne_dist:
            abschnitt("Ziel A: Distributions-Upgrade bookworm -> trixie")
            d = gui.api("/api/dist")
            pruefe(d.get("moeglich") and d.get("neu") == "trixie", f"dist-info: {d.get('alt')} -> {d.get('neu')}, umzustellen: {d.get('quellen_neu')}")
            vorher = dexec(n, "md5sum /etc/apt/sources.list.d/debian.sources")
            r = gui.folgen(gui.api("/api/dist-simulieren", {}))
            e = r.get("ergebnis") or {}
            pruefe(r["status"] == "ok" and e.get("suite") == "stable" and len(e.get("aktualisiert", [])) > 50,
                   f"Simulation: Suite {e.get('suite')}, {len(e.get('aktualisiert', []))} aktualisiert, "
                   f"{len(e.get('neu', []))} neu, {len(e.get('entfernt', []))} entfernt, {e.get('bytes', 0) // 1048576} MiB")
            pruefe(vorher == dexec(n, "md5sum /etc/apt/sources.list.d/debian.sources")
                   and dexec(n, "cat /etc/debian_version").startswith("12"), "Simulation hat am System nichts geaendert")
            r = gui.api("/api/dist-ausfuehren", {"bestaetigung": "nein"}, erwarte=400)
            pruefe("UPGRADE" in r["fehler"], "ohne Bestaetigung kein Upgrade")
            t0 = time.time()
            r = gui.folgen(gui.api("/api/dist-ausfuehren", {"bestaetigung": "UPGRADE"}))
            dv = dexec(n, "cat /etc/debian_version")
            pruefe(r["status"] == "ok" and dv.startswith("13"),
                   f"Distributions-Upgrade in {time.time() - t0:.0f} s: debian_version {dv}" + ("" if r["status"] == "ok" else f" -- {r['fehler']}"))
            i = gui.api("/api/info")
            pruefe(i.get("codename") == "trixie" and i.get("dpkg_ok") == "ja", f"danach: {i.get('os')}, dpkg_ok={i.get('dpkg_ok')}")
            pruefe(dexec(n, "ls /var/lib/apt-via-ssh/quellen-vor-trixie-*.tar | wc -l") == "1", "Sicherung der alten Quellen vorhanden")
            # Auf echten Systemen startet openssh-server sshd beim Upgrade neu (systemd); im Container
            # verhindert policy-rc.d das -- hier nachbilden, dann eine NEUE Verbindung aufbauen.
            dexec(n, "kill -HUP 1")
            time.sleep(1)
            gui.api("/api/trennen", {})
            z2 = gui.verbinden({"host": ip, "modus": "wartung"})
            pruefe(z2.get("rolle") == "wartung", "nach dem Upgrade: neue SSH-Verbindung als Wartungsbenutzer moeglich")
            r = gui.folgen(gui.api("/api/pruefen", {}))
            pruefe(r["status"] == "ok", f"Updates unter trixie pruefbar ({len(r['ergebnis']['aktualisiert']) if r['status'] == 'ok' else '?'} offen)")
        gui.api("/api/trennen", {})

        # --------------------------------------------- Ziel B: su, kein sudo
        n, ip = container["su"]
        abschnitt(f"Ziel B ({ip}): kein sudo, Weiterleitung gesperrt, root nur per su")
        z = gui.verbinden({"host": ip, "benutzer": "verwalter", "passwort": PW_VERWALTER, "root_passwort": PW_ROOT})
        pruefe(z.get("rolle") == "su", f"Erstzugang: Rolle {z.get('rolle')}")
        v = gui.api("/api/voraussetzungen")
        st = {s["schritt"]: s["status"] for s in v["schritte"]}
        pruefe(st.get("Tunnel") == "FEHLER" and st.get("sudo") == "FEHLER" and st.get("Weiterleitung") == "FEHLER",
               f"erkannt: Tunnel {st.get('Tunnel')}, sudo {st.get('sudo')}, Weiterleitung {st.get('Weiterleitung')}")
        r = gui.folgen(gui.api("/api/einrichten", {"wart_benutzer": "aptpflege", "weiterleitung": True}), zeigen=True)
        pruefe(r["status"] == "ok", "zweistufige Einrichtung erfolgreich" + ("" if r["status"] == "ok" else f": {r['fehler']}"))
        pruefe(dexec(n, "dpkg -s sudo >/dev/null 2>&1 && echo ja") == "ja", "sudo wurde ueber den Tunnel des Wartungsbenutzers installiert")
        fw = dexec(n, "sshd -T -C user=verwalter,host=x,addr=127.0.0.1 | grep -i ^allowtcpforwarding; "
                      "sshd -T -C user=aptpflege,host=x,addr=127.0.0.1 | grep -i ^allowtcpforwarding")
        pruefe(fw.split() == ["allowtcpforwarding", "no", "allowtcpforwarding", "remote"],
               f"Weiterleitung nur fuer aptpflege freigeschaltet ({' / '.join(fw.splitlines())})")
        z = gui.verbinden({"host": ip, "modus": "wartung"})
        pruefe(z.get("rolle") == "wartung", "als Wartungsbenutzer verbunden")
        r = gui.folgen(gui.api("/api/pruefen", {}))
        pruefe(r["status"] == "ok" and r["ergebnis"]["aktualisiert"], f"Updates gefunden: {len(r['ergebnis']['aktualisiert']) if r['status'] == 'ok' else '?'}")

        abschnitt("Ziel B: Verbindungsabbruch waehrend der Installation")
        a = gui.api("/api/ausfuehren", {"modus": "full-upgrade", "pakete": []})
        while True:   # warten, bis die Installation auf dem Ziel abgekoppelt laeuft
            rr = gui.api(f"/api/auftrag?id={a['id']}&ab=0")
            if any("unabhängig" in z for z in rr["zeilen"]) or rr["status"] != "laeuft":
                break
            time.sleep(0.5)
        time.sleep(1)
        gui.api("/api/trennen", {})          # Verbindung hart kappen
        gut("SSH-Verbindung waehrend der Installation getrennt")
        r = gui.folgen(a)
        pruefe(r["status"] == "fehler", f"Oberflaeche meldet den Abbruch: {r['fehler'][:90]}")
        z = gui.verbinden({"host": ip, "modus": "wartung"})
        r = gui.folgen(gui.api("/api/verfolgen", {}))
        pruefe(r["status"] == "ok", "nach dem Neuverbinden: Auftrag auf dem Ziel erfolgreich zu Ende gelaufen")
        offen = dexec(n, "apt-get -s upgrade | grep -c ^Inst")
        pruefe(offen == "0", f"auf dem Ziel keine Updates mehr offen ({offen})")

        abschnitt("Ziel B: Kommandozeile mit dem Wartungsbenutzer")
        p = json.loads((tmp / "config" / "apt-via-ssh" / "ziele.json").read_text())
        schl = next(x["wart_schluessel"] for x in p if x["host"] == ip)
        cli = [str(PROJEKT / "bin" / "apt-via-ssh"), "-i", schl, "-o", "BatchMode=yes",
               "-o", f"UserKnownHostsFile={tmp / 'config' / 'apt-via-ssh' / 'known_hosts'}",
               "--cache", str(tmp / "cache" / "cli")]
        r = sh(*cli, "-y", f"aptpflege@{ip}", "install", "htop", timeout=600)
        pruefe(r.returncode == 0 and dexec(n, "dpkg -s htop >/dev/null && echo ja") == "ja",
               "apt-via-ssh -y aptpflege@ziel install htop (Helfer-Modus)")
        r = sh(*cli, f"aptpflege@{ip}", "run", "id", timeout=120)
        pruefe(r.returncode != 0 and "gesperrt" in r.stderr, "run ist fuer den Wartungsbenutzer gesperrt")
        try:   # -W oeffnet sofort einen direct-tcpip-Kanal: muss abgelehnt werden
            r = sh("ssh", "-i", schl, "-o", "BatchMode=yes", "-o", f"UserKnownHostsFile={tmp / 'config' / 'apt-via-ssh' / 'known_hosts'}",
                   "-W", "127.0.0.1:22", f"aptpflege@{ip}", input="", timeout=20)
            pruefe(r.returncode != 0 and "prohibited" in r.stderr, "Wartungsschluessel: keine Weiterleitung ins Netz des Ziels (-W/-L abgelehnt)")
        except subprocess.TimeoutExpired:
            schlecht("Wartungsschluessel: -W wurde NICHT abgelehnt")
        r = sh("ssh", "-i", schl, "-o", "BatchMode=yes", "-o", f"UserKnownHostsFile={tmp / 'config' / 'apt-via-ssh' / 'known_hosts'}",
               f"aptpflege@{ip}", "sudo -n /usr/bin/id", timeout=20)
        pruefe(r.returncode != 0, "Wartungsbenutzer: sudo fuer andere Befehle verweigert")

        abschnitt("Ziel B: Wartungszugang entfernen")
        gui.verbinden({"host": ip, "benutzer": "verwalter", "passwort": PW_VERWALTER, "root_passwort": PW_ROOT})
        r = gui.folgen(gui.api("/api/entfernen", {}))
        rest = dexec(n, "id aptpflege 2>/dev/null; ls /etc/sudoers.d/apt-via-ssh /usr/local/sbin/apt-via-ssh-helper "
                        "/etc/ssh/sshd_config.d/50-apt-via-ssh.conf 2>/dev/null")
        pruefe(r["status"] == "ok" and rest == "", f"Benutzer, sudo-Regel, Helfer und sshd-Freischaltung entfernt {rest!r}")
        gui.api("/api/trennen", {})
    except Exception as e:  # noqa: BLE001
        import traceback; traceback.print_exc()
        schlecht(f"Abbruch: {e}")
    finally:
        if gui:
            gui.ende()
        for n, _ in container.values():
            sh("docker", "rm", "-f", n)
        sh("docker", "network", "rm", NETZ)
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nErgebnis: {ok} OK, {fehl} fehlgeschlagen")
    return 0 if fehl == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
