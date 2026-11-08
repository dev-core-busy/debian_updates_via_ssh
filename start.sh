#!/usr/bin/env bash
#
# start.sh -- startet die apt-via-ssh-Oberflaeche im Browser.
#
#   ./start.sh                 Oberflaeche starten und Browser oeffnen
#   ./start.sh --kein-browser  nur starten, Link wird ausgegeben
#   ./start.sh --port 9000     anderer Port (Vorgabe 8765, belegt -> frei)
#
# Funktioniert auch ueber einen Symlink, z.B.  ln -s "$PWD/start.sh" ~/bin/apt-via-ssh-gui
set -euo pipefail

HIER="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"

fehlt=()
for b in python3 ssh ssh-keygen ssh-keyscan; do
    command -v "$b" >/dev/null 2>&1 || fehlt+=("$b")
done
if [ ${#fehlt[@]} -gt 0 ]; then
    echo "Fehlt: ${fehlt[*]} -- bitte installieren, z.B.: sudo apt install python3 openssh-client" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Python 3.9 oder neuer wird benoetigt (gefunden: $(python3 -V 2>&1))" >&2
    exit 1
fi

exec python3 "$HIER/gui/server.py" "$@"
