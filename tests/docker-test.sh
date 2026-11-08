#!/usr/bin/env bash
# Integrationstest mit echtem, abgeschottetem Zielsystem (Docker).
#
#   tests/docker-test.sh                    # Vorgabe: Debian 12 vom Mai 2024 (viele Updates offen)
#   DEBIAN_TAG=trixie tests/docker-test.sh  # aktuelles Debian 13
#
# Baut ein Debian-Image mit sshd, startet zwei Container in einem Docker-Netz
# OHNE Internet (--internal):
#   ziel-a  normaler sshd                  -> apt-via-ssh
#   ziel-b  sshd mit AllowTcpForwarding no -> Fehlermeldung + apt-offline-sync
# Am Ende werden Container und Netz entfernt (das Image bleibt fuer Wiederholungen).
set -uo pipefail
HIER="$(cd -- "$(dirname -- "$0")/.." && pwd)"
TAG="${DEBIAN_TAG:-bookworm-20240513}"
REL="${TAG%%-*}"
IMG="apt-via-ssh-ziel:$TAG"; NETZ="apt-via-ssh-iso"; A="apt-via-ssh-ziel-a"; B="apt-via-ssh-ziel-b"
TMP="$(mktemp -d -t apt-via-ssh-dockertest.XXXXXX)"
ok=0; fehl=0
gut()  { printf '  \033[32mOK\033[0m    %s\n' "$*"; ok=$((ok+1)); }
schlecht(){ printf '  \033[31mFEHL\033[0m  %s\n' "$*"; fehl=$((fehl+1)); }
abschnitt(){ printf '\n== %s ==\n' "$*"; }
aufraeumen() { docker rm -f "$A" "$B" >/dev/null 2>&1; docker network rm "$NETZ" >/dev/null 2>&1; rm -rf "$TMP"; }
trap aufraeumen EXIT

abschnitt "Vorbereitung (debian:$TAG)"
command -v docker >/dev/null && docker info >/dev/null 2>&1 || { echo "Docker laeuft nicht"; exit 2; }
docker build -q --build-arg DEBIAN_TAG="$TAG" -t "$IMG" "$HIER/tests/docker" >/dev/null || { echo "Build fehlgeschlagen"; exit 2; }
# Reste eines abgebrochenen Laufs entfernen
docker rm -f "$A" "$B" >/dev/null 2>&1; docker network rm "$NETZ" >/dev/null 2>&1
ssh-keygen -q -t ed25519 -N '' -f "$TMP/key" -C apt-via-ssh-test
docker network create --internal "$NETZ" >/dev/null
docker run -d --name "$A" --network "$NETZ" "$IMG" >/dev/null
docker run -d --name "$B" --network "$NETZ" "$IMG" /usr/sbin/sshd -D -e -o AllowTcpForwarding=no >/dev/null
for c in "$A" "$B"; do
    docker cp "$TMP/key.pub" "$c:/root/.ssh/authorized_keys"
    docker exec "$c" sh -c 'chown root:root /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys;
        cp /root/.ssh/authorized_keys /home/admin/.ssh/; chown admin:admin /home/admin/.ssh/authorized_keys'
done
IP_A="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$A")"
IP_B="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$B")"
echo "  ziel-a $IP_A, ziel-b $IP_B"
SSHO=(-i "$TMP/key" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
# apt-via-ssh / apt-offline-sync erwarten ssh-Optionen als -o bzw. -i
# Jeder Aufruf mit Zeitlimit: ein Haenger zaehlt als Fehlschlag statt zu blockieren.
AVS=(timeout 900 "$HIER/bin/apt-via-ssh" -i "$TMP/key" -o BatchMode=yes -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR --cache "$TMP/cache")
AOS=(timeout 900 "$HIER/bin/apt-offline-sync" -i "$TMP/key" -o BatchMode=yes -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)
for _ in $(seq 1 30); do ssh "${SSHO[@]}" -o ConnectTimeout=2 "root@$IP_A" true 2>/dev/null && break; sleep 0.5; done

abschnitt "Isolation des Ziels"
if docker exec "$A" timeout 8 bash -c 'exec 3<>/dev/tcp/151.101.2.132/80' 2>/dev/null; then
    schlecht "ziel-a erreicht das Internet -- Test ungueltig"; exit 1
else gut "ziel-a erreicht 151.101.2.132:80 (deb.debian.org) nicht"; fi
if docker exec "$A" timeout 30 apt-get -q update >"$TMP/iso.log" 2>&1 && ! grep -qE '^(W|E|Err|Ign):' "$TMP/iso.log"; then
    schlecht "apt-get update ohne Proxy hat funktioniert -- Test ungueltig"; exit 1
else gut "apt-get update direkt auf ziel-a scheitert ($(grep -cE '^(Err|Ign|W|E):' "$TMP/iso.log") Fehlerzeilen)"; fi
docker exec "$A" sh -c 'command -v curl wget python3' >/dev/null 2>&1 \
    && echo "  (Hinweis: curl/wget/python3 vorhanden)" || gut "kein curl/wget/python3 -> Tunnelpruefung ueber /dev/tcp"

abschnitt "apt-via-ssh gegen ziel-a"
"${AVS[@]}" "root@$IP_A" selftest >"$TMP/1.log" 2>&1 && grep -q "Selbsttest erfolgreich" "$TMP/1.log" \
    && gut "selftest" || { schlecht "selftest"; tail -15 "$TMP/1.log"; }
"${AVS[@]}" "root@$IP_A" check >"$TMP/2.log" 2>&1 && gut "check ($(grep -c 'aktualisierbar\|upgradable' "$TMP/2.log") Updates angezeigt)" \
    || { schlecht "check"; tail -10 "$TMP/2.log"; }
VORHER=$(ssh "${SSHO[@]}" "root@$IP_A" "apt-get -s upgrade 2>/dev/null | grep -c '^Inst '")
echo "  ($VORHER Pakete vor dem Upgrade offen)"
"${AVS[@]}" -y "root@$IP_A" upgrade >"$TMP/3.log" 2>&1 && gut "upgrade -y (echt)" || { schlecht "upgrade -y"; tail -15 "$TMP/3.log"; }
N=$(ssh "${SSHO[@]}" "root@$IP_A" "apt-get -s upgrade 2>/dev/null | grep -c '^Inst '")
[ "$N" = 0 ] && gut "nach dem Upgrade: 0 ausstehende Updates ($VORHER Pakete erneuert)" || schlecht "nach dem Upgrade noch $N Updates offen"
"${AVS[@]}" -y "root@$IP_A" install sl >"$TMP/4.log" 2>&1 \
    && docker exec "$A" dpkg -s sl >/dev/null 2>&1 && gut "install -y sl (Paket ist installiert)" || { schlecht "install sl"; tail -10 "$TMP/4.log"; }
"${AVS[@]}" -y --sudo "admin@$IP_A" install less >"$TMP/5.log" 2>&1 \
    && docker exec "$A" dpkg -s less >/dev/null 2>&1 && gut "install als admin mit sudo (env-Weitergabe)" || { schlecht "admin+sudo"; tail -10 "$TMP/5.log"; }
docker exec "$A" sh -c 'apt-get -y -q purge sl >/dev/null 2>&1; rm -f /var/cache/apt/archives/*.deb'
"${AVS[@]}" -y -v "root@$IP_A" install sl >"$TMP/6.log" 2>&1
ls "$TMP"/cache/deb.debian.org/debian/pool/main/s/sl/*.deb >/dev/null 2>&1 \
    && docker exec "$A" dpkg -s sl >/dev/null 2>&1 && gut "Paket-Cache auf dem Gateway gefuellt, erneute Installation ok" \
    || { schlecht "Cache"; tail -10 "$TMP/6.log"; }
# run mit mehreren Argumenten (bash -c SKRIPT): Abruf ueber $http_proxy per /dev/tcp
HOLEN='exec 3<>/dev/tcp/127.0.0.1/${http_proxy##*:}; printf "GET http://deb.debian.org/debian/dists/'"$REL"'/Release HTTP/1.0\r\n\r\n" >&3; head -1 <&3'
OUT="$("${AVS[@]}" "root@$IP_A" run bash -c "$HOLEN" 2>&1)"
grep -q "HTTP/1.[01] 200" <<<"$OUT" && gut "run (mehrere Argumente): Abruf ueber den Tunnel" || { schlecht "run mehrere Argumente"; tail -5 <<<"$OUT"; }
OUT="$("${AVS[@]}" "root@$IP_A" run apt-cache policy sl 2>&1)"
grep -qE "Installed: [0-9]|Installiert: [0-9]" <<<"$OUT" && gut "run apt-cache policy sl (Argumente kommen an)" || { schlecht "run Argumente"; tail -5 <<<"$OUT"; }
OUT="$("${AVS[@]}" "root@$IP_A" run 'echo "$http_proxy" | grep -c 127.0.0.1' 2>&1)"
grep -qx "1" <<<"$OUT" && gut "run (ein Argument als Shell-Skript mit Pipe)" || { schlecht "run Skript"; tail -5 <<<"$OUT"; }
"${AVS[@]}" "root@$IP_A" install -y gibtsnicht-xyz >"$TMP/7.log" 2>&1 \
    && schlecht "unbekanntes Paket haette fehlschlagen muessen" || gut "unbekanntes Paket -> Exit-Code != 0"
LEFT=$(ssh "${SSHO[@]}" "root@$IP_A" 'ss -ltn 2>/dev/null | grep -c 127.0.0.1 || netstat -ltn 2>/dev/null | grep -c 127.0.0.1; ls /etc/apt/apt.conf.d' 2>/dev/null)
grep -qi proxy <<<"$LEFT" && schlecht "Proxy-Datei auf dem Ziel hinterlassen" || gut "auf dem Ziel keine Konfiguration hinterlassen"

abschnitt "ziel-b: AllowTcpForwarding no"
"${AVS[@]}" "root@$IP_B" selftest >"$TMP/8.log" 2>&1 \
    && schlecht "apt-via-ssh haette scheitern muessen" \
    || { grep -q "AllowTcpForwarding" "$TMP/8.log" && gut "verstaendliche Fehlermeldung mit Verweis auf apt-offline-sync" \
         || { schlecht "Fehlermeldung unklar"; tail -8 "$TMP/8.log"; }; }

abschnitt "apt-offline-sync gegen ziel-b"
"${AOS[@]}" "root@$IP_B" check >"$TMP/9.log" 2>&1 && gut "check ($(grep -oE '[0-9]+ Paket\(e\)|aktuell' "$TMP/9.log" | head -1))" \
    || { schlecht "check"; tail -15 "$TMP/9.log"; }
"${AOS[@]}" -y "root@$IP_B" upgrade >"$TMP/10.log" 2>&1 \
    && gut "upgrade -y (echt, $(grep -oE '[0-9]+ Paket\(e\), [0-9]+ MiB' "$TMP/10.log" | head -1) per tar uebertragen)" \
    || { schlecht "upgrade"; tail -15 "$TMP/10.log"; }
N=$(ssh "${SSHO[@]}" "root@$IP_B" "apt-get -s upgrade 2>/dev/null | grep -c '^Inst '")
[ "$N" = 0 ] && gut "nach dem Upgrade: 0 ausstehende Updates (Listen kamen per tar)" || schlecht "noch $N Updates offen"
"${AOS[@]}" -y "root@$IP_B" install sl >"$TMP/11.log" 2>&1 \
    && docker exec "$B" dpkg -s sl >/dev/null 2>&1 && gut "install -y sl" || { schlecht "install sl"; tail -15 "$TMP/11.log"; }
docker exec "$B" sh -c 'ls -d /var/lib/apt/lists.vor-apt-offline-sync >/dev/null && ! ls -d /var/tmp/apt-offline-sync.* 2>/dev/null' \
    && gut "Listen-Backup vorhanden, kein Staging-Rest" || schlecht "Backup/Staging"

abschnitt "Gateway"
ls -d /tmp/apt-via-ssh.* /tmp/apt-offline-sync.* 2>/dev/null | grep -q . && schlecht "Arbeitsverzeichnisse uebrig" || gut "keine Arbeitsverzeichnisse uebrig"
pgrep -f "python3 .*apt_proxy.py" >/dev/null && schlecht "Proxy laeuft noch" || gut "kein Proxy-Prozess uebrig"

printf '\nErgebnis: %d OK, %d fehlgeschlagen\n' "$ok" "$fehl"
[ "$fehl" -eq 0 ]
