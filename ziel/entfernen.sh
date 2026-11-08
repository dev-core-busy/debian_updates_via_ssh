# apt-via-ssh: Wartungszugang wieder entfernen (laeuft als root).
# Variable: WART_BENUTZER
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
U="${WART_BENUTZER:-aptpflege}"
[[ "$U" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || { echo "FEHLER Benutzer: ungueltiger Name"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "FEHLER Rechte: nicht root"; exit 1; }
if id "$U" >/dev/null 2>&1; then
    [ "$(id -u "$U")" -ne 0 ] || { echo "FEHLER Benutzer: UID 0, abgebrochen"; exit 1; }
    pkill -u "$U" 2>/dev/null; sleep 1
    userdel -r "$U" 2>/dev/null || userdel "$U"
    echo "OK Benutzer: $U entfernt"
else echo "OK Benutzer: $U war nicht vorhanden"; fi
rm -f /etc/sudoers.d/apt-via-ssh && echo "OK sudo-Regel: entfernt"
rm -f /usr/local/sbin/apt-via-ssh-helper && echo "OK Hilfsskript: entfernt"
GEAENDERT=0
[ -f /etc/ssh/sshd_config.d/50-apt-via-ssh.conf ] && rm -f /etc/ssh/sshd_config.d/50-apt-via-ssh.conf && GEAENDERT=1
if grep -q '^# BEGIN apt-via-ssh$' /etc/ssh/sshd_config 2>/dev/null; then
    sed -i '/^# BEGIN apt-via-ssh$/,/^# END apt-via-ssh$/d' /etc/ssh/sshd_config; GEAENDERT=1
fi
if [ "$GEAENDERT" = 1 ]; then
    if sshd -t; then
        systemctl reload ssh 2>/dev/null || service ssh reload 2>/dev/null \
          || { [ -s /run/sshd.pid ] && kill -HUP "$(cat /run/sshd.pid)"; } || pkill -HUP -o -x sshd
        echo "OK Weiterleitung: Freischaltung entfernt"
    else echo "FEHLER Weiterleitung: sshd -t meldet Fehler"; fi
fi
echo "INFO Protokolle: /var/log/apt-via-ssh bleibt erhalten"
echo "ERGEBNIS ok"
