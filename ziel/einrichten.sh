# apt-via-ssh: Wartungszugang auf dem Zielsystem einrichten (laeuft als root).
#
# Die GUI stellt diesem Skript folgende Variablen voran:
#   WART_BENUTZER   Name des Wartungsbenutzers (z.B. aptpflege)
#   WART_SCHLUESSEL oeffentlicher SSH-Schluessel (eine Zeile)
#   SCHLUESSEL_OPT  Optionen fuer authorized_keys
#   HELFER_B64      Hilfsskript, base64-kodiert
#   HELFER_VERSION  erwartete Version des Hilfsskripts
#   WEITERLEITUNG   1 = Port-Weiterleitung fuer den Wartungsbenutzer freischalten
#   PROXY           http://127.0.0.1:PORT (Tunnel), um z.B. sudo nachzuinstallieren
#   NUR_PRUEFEN     1 = nichts aendern, nur den Zustand melden
#
# Ausgabeformat (je Zeile):  OK|NEU|WARNUNG|FEHLER|INFO <Schritt>: <Text>
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C.UTF-8
U="${WART_BENUTZER:-aptpflege}"
P="${NUR_PRUEFEN:-0}"
HELFER=/usr/local/sbin/apt-via-ssh-helper
SUDOERS=/etc/sudoers.d/apt-via-ssh
DROPIN=/etc/ssh/sshd_config.d/50-apt-via-ssh.conf
FEHLER=0

meld() { printf '%s %s: %s\n' "$1" "$2" "$3"; [ "$1" = FEHLER ] && FEHLER=1; return 0; }

[[ "$U" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || { meld FEHLER Benutzer "ungültiger Name: $U"; exit 1; }
[ "$(id -u)" = 0 ] || { meld FEHLER Rechte "Skript läuft nicht als root"; exit 1; }

# ----------------------------------------------------------------- System
. /etc/os-release 2>/dev/null
meld INFO System "${PRETTY_NAME:-unbekannt}, $(dpkg --print-architecture 2>/dev/null)"
command -v apt-get >/dev/null || { meld FEHLER System "apt-get fehlt – kein Debian-System?"; exit 1; }
FREI=$(df -Pm /var | awk 'NR==2{print $4}')
if [ "$FREI" -lt 500 ]; then meld WARNUNG Speicher "nur $FREI MiB frei in /var"
else meld OK Speicher "$FREI MiB frei in /var"; fi

# ------------------------------------------------------------------- sudo
if command -v sudo >/dev/null; then
    meld OK sudo "installiert"
elif [ "$P" = 1 ]; then
    meld FEHLER sudo "nicht installiert (wird beim Einrichten über den Tunnel installiert)"
elif [ -n "${PROXY:-}" ]; then
    PO=(-o "Acquire::http::Proxy=$PROXY" -o "Acquire::https::Proxy=$PROXY")
    if apt-get -q "${PO[@]}" update >/tmp/apt-via-ssh-sudo.log 2>&1 \
       && DEBIAN_FRONTEND=noninteractive apt-get -q -y "${PO[@]}" install sudo >>/tmp/apt-via-ssh-sudo.log 2>&1; then
        meld NEU sudo "über den Tunnel installiert"
    else
        meld FEHLER sudo "Installation fehlgeschlagen: $(tail -n 2 /tmp/apt-via-ssh-sudo.log | tr '\n' ' ')"
    fi
    rm -f /tmp/apt-via-ssh-sudo.log
else
    meld FEHLER sudo "nicht installiert und kein Tunnel verfügbar"
fi

# --------------------------------------------------------------- Benutzer
if id "$U" >/dev/null 2>&1; then
    [ "$(id -u "$U")" -ne 0 ] || { meld FEHLER Benutzer "$U hat UID 0 – abgebrochen"; exit 1; }
    meld OK Benutzer "$U vorhanden"
elif [ "$P" = 1 ]; then
    meld FEHLER Benutzer "$U fehlt"
else
    if useradd -m -s /bin/bash -c "apt-via-ssh Wartung" "$U"; then meld NEU Benutzer "$U angelegt"
    else meld FEHLER Benutzer "useradd fehlgeschlagen"; exit 1; fi
fi
if id "$U" >/dev/null 2>&1 && [ "$P" != 1 ]; then
    # '*' = kein gueltiges Passwort, aber NICHT gesperrt (sonst lehnt sshd ohne PAM auch Schluessel ab)
    usermod -p '*' "$U"
fi

# ----------------------------------------------------------- SSH-Schluessel
if id "$U" >/dev/null 2>&1; then
    H="$(getent passwd "$U" | cut -d: -f6)"; G="$(id -gn "$U")"
    AK="$H/.ssh/authorized_keys"
    if [ -n "${WART_SCHLUESSEL:-}" ]; then
        ZEILE="${SCHLUESSEL_OPT:+$SCHLUESSEL_OPT }$WART_SCHLUESSEL"
        KOMMENTAR="${WART_SCHLUESSEL##* }"
        if [ -f "$AK" ] && grep -qxF "$ZEILE" "$AK"; then
            meld OK Schlüssel "hinterlegt"
        elif [ "$P" = 1 ]; then
            meld FEHLER Schlüssel "nicht hinterlegt"
        else
            install -d -m 700 -o "$U" -g "$G" "$H/.ssh"
            touch "$AK"
            # alte Zeile mit demselben Kommentar ersetzen
            grep -vF " $KOMMENTAR" "$AK" > "$AK.neu" || true
            printf '%s\n' "$ZEILE" >> "$AK.neu"
            mv "$AK.neu" "$AK"; chown "$U:$G" "$AK"; chmod 600 "$AK"
            meld NEU Schlüssel "in $AK eingetragen"
        fi
    fi
fi

# ------------------------------------------------------------ Hilfsskript
if [ -x "$HELFER" ] && [ "$("$HELFER" version 2>/dev/null)" = "${HELFER_VERSION:-x}" ]; then
    meld OK Hilfsskript "Version ${HELFER_VERSION} installiert"
elif [ "$P" = 1 ]; then
    [ -x "$HELFER" ] && meld FEHLER Hilfsskript "veraltet (Version $("$HELFER" version 2>/dev/null))" \
                     || meld FEHLER Hilfsskript "fehlt"
else
    T="$(mktemp)"
    if printf '%s' "${HELFER_B64:-}" | base64 -d > "$T" 2>/dev/null && bash -n "$T"; then
        install -m 0755 -o root -g root "$T" "$HELFER"
        meld NEU Hilfsskript "nach $HELFER installiert"
    else
        meld FEHLER Hilfsskript "Übertragung beschädigt"
    fi
    rm -f "$T"
fi

# ------------------------------------------------------------- sudo-Regel
SOLL="# Verwaltet von apt-via-ssh -- erlaubt NUR das Hilfsskript
Defaults:$U !requiretty
$U ALL=(root) NOPASSWD: $HELFER"
if [ -f "$SUDOERS" ] && [ "$(cat "$SUDOERS")" = "$SOLL" ]; then
    meld OK sudo-Regel "$SUDOERS vorhanden"
elif [ "$P" = 1 ]; then
    meld FEHLER sudo-Regel "fehlt oder abweichend"
else
    T="$(mktemp)"; printf '%s\n' "$SOLL" > "$T"
    if ! command -v visudo >/dev/null; then
        # sudo (und visudo) fehlt noch: Regel vorab ablegen, Pruefung beim naechsten Lauf
        install -d -m 0755 /etc/sudoers.d
        install -m 0440 -o root -g root "$T" "$SUDOERS"; meld WARNUNG sudo-Regel "angelegt, aber ungeprüft (visudo fehlt)"
    elif visudo -cqf "$T"; then
        install -m 0440 -o root -g root "$T" "$SUDOERS"; meld NEU sudo-Regel "$SUDOERS angelegt"
    else
        meld FEHLER sudo-Regel "visudo lehnt die Regel ab"
    fi
    rm -f "$T"
fi
if [ -f /etc/sudoers ] && ! grep -qE '^[@#]includedir[[:space:]]+/etc/sudoers.d' /etc/sudoers; then
    meld WARNUNG sudo-Regel "/etc/sudoers bindet /etc/sudoers.d nicht ein"
fi
if command -v sudo >/dev/null && id "$U" >/dev/null 2>&1; then
    V="$(sudo -u "$U" sudo -n "$HELFER" version 2>/dev/null)"
    if [ -n "$V" ]; then meld OK sudo-Test "$U darf das Hilfsskript ausführen"
    else meld FEHLER sudo-Test "$U kann das Hilfsskript nicht per sudo ausführen"; fi
    if sudo -u "$U" sudo -n true 2>/dev/null; then
        meld WARNUNG sudo-Test "$U hat weitergehende sudo-Rechte als nötig"
    fi
fi

# ------------------------------------------------------------------- sshd
sshd_wert() { sshd -T -C "user=$U,host=localhost,addr=127.0.0.1" 2>/dev/null | awk -v k="$1" '$1==k{ $1=""; sub(/^ /,""); print; exit }'; }
sshd_neu_laden() {
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null \
      || service ssh reload 2>/dev/null \
      || { [ -s /run/sshd.pid ] && kill -HUP "$(cat /run/sshd.pid)"; } \
      || pkill -HUP -o -x sshd
}
if ! command -v sshd >/dev/null; then
    meld WARNUNG sshd "sshd nicht im PATH, Konfiguration nicht prüfbar"
else
    # Soll: der Wartungsbenutzer darf NUR den Reverse-Tunnel (-R) aufbauen,
    # keine lokalen Weiterleitungen (-L/-W), also das Ziel nicht als Sprungbrett nutzen.
    W="$(sshd_wert allowtcpforwarding)"; D="$(sshd_wert disableforwarding)"; PO="$(sshd_wert permitopen)"
    if [ "$W" = remote ] && [ "$PO" = none ] && [ "$D" != yes ]; then
        meld OK Weiterleitung "für $U nur Reverse-Tunnel erlaubt"
    elif [ "$P" = 1 ] || [ "${WEITERLEITUNG:-0}" != 1 ]; then
        case "$W" in
            yes|all) [ "$D" != yes ] && meld WARNUNG Weiterleitung "für $U offen, aber nicht auf den Reverse-Tunnel beschränkt ($W)" \
                                     || meld FEHLER Weiterleitung "für $U gesperrt (DisableForwarding=yes)" ;;
            remote)  meld WARNUNG Weiterleitung "Reverse-Tunnel möglich, PermitOpen=$PO" ;;
            *)       meld FEHLER Weiterleitung "für $U gesperrt (AllowTcpForwarding=$W${D:+, DisableForwarding=$D})" ;;
        esac
    else
        BLOCK="Match User $U
    AllowTcpForwarding remote
    DisableForwarding no
    PermitOpen none
    AllowStreamLocalForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no"
        if grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
            printf '# Verwaltet von apt-via-ssh\n%s\n' "$BLOCK" > "$DROPIN"; ORT="$DROPIN"
        else
            sed -i '/^# BEGIN apt-via-ssh$/,/^# END apt-via-ssh$/d' /etc/ssh/sshd_config
            printf '\n# BEGIN apt-via-ssh\n%s\n# END apt-via-ssh\n' "$BLOCK" >> /etc/ssh/sshd_config; ORT=/etc/ssh/sshd_config
        fi
        if sshd -t 2>/tmp/apt-via-ssh-sshd.err; then
            sshd_neu_laden
            W="$(sshd_wert allowtcpforwarding)"; PO="$(sshd_wert permitopen)"
            if [ "$W" = remote ] && [ "$PO" = none ]; then
                meld NEU Weiterleitung "für $U auf den Reverse-Tunnel beschränkt ($ORT)"
            else
                meld FEHLER Weiterleitung "Regel in $ORT greift nicht (AllowTcpForwarding=$W, PermitOpen=$PO)"
            fi
        else
            meld FEHLER Weiterleitung "sshd -t meldet Fehler, Änderung zurückgenommen: $(head -1 /tmp/apt-via-ssh-sshd.err)"
            if [ "$ORT" = "$DROPIN" ]; then rm -f "$DROPIN"
            else sed -i '/^# BEGIN apt-via-ssh$/,/^# END apt-via-ssh$/d' /etc/ssh/sshd_config; fi
        fi
        rm -f /tmp/apt-via-ssh-sshd.err
    fi
    PK="$(sshd_wert pubkeyauthentication)"
    [ "$PK" = no ] && meld FEHLER Anmeldung "PubkeyAuthentication ist deaktiviert"
    AU="$(sshd_wert allowusers)"; AG="$(sshd_wert allowgroups)"
    if [ -n "$AU" ] && ! printf '%s\n' $AU | grep -qxE "$U(@.*)?"; then
        meld WARNUNG Anmeldung "AllowUsers erlaubt $U nicht – bitte in sshd_config ergänzen"
    fi
    [ -n "$AG" ] && meld INFO Anmeldung "AllowGroups aktiv: $AG"
fi

[ "$FEHLER" = 0 ] && echo "ERGEBNIS ok" || echo "ERGEBNIS fehler"
exit "$FEHLER"
