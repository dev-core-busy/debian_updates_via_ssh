#!/usr/bin/env bash
# Rauchtest gegen ein erreichbares SSH-Ziel (Vorgabe: localhost).
# Veraendert das Ziel NICHT: nur apt-get update und Simulationen (-s).
#
#   tests/rauchtest.sh [ZIEL] [weitere apt-via-ssh-Optionen, z.B. --allow HOST]
set -uo pipefail
HIER="$(cd -- "$(dirname -- "$0")/.." && pwd)"
ZIEL="${1:-localhost}"; shift || true
O=(-o BatchMode=yes "$@")
ok=0; fehl=0
pruefe() {   # pruefe "Name" erwartetes-Muster befehl...
    local name="$1" muster="$2"; shift 2
    local aus; aus="$("$@" 2>&1)"; local rc=$?
    if [ $rc -eq 0 ] && grep -qE "$muster" <<<"$aus"; then
        printf '  OK    %s\n' "$name"; ok=$((ok+1))
    else
        printf '  FEHL  %s (rc=%s)\n' "$name" "$rc"; tail -n 5 <<<"$aus" | sed 's/^/        /'; fehl=$((fehl+1))
    fi
}
echo "Rauchtest gegen $ZIEL"
pruefe "Proxy-Syntax"         "^ok$"                  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read()); print('ok')" "$HIER/gateway/apt_proxy.py"
pruefe "selftest"             "Selbsttest erfolgreich" "$HIER/bin/apt-via-ssh" "${O[@]}" "$ZIEL" selftest
pruefe "check"                "Anstehende Updates"     "$HIER/bin/apt-via-ssh" "${O[@]}" "$ZIEL" check
pruefe "upgrade -s"           "Fertig"                 "$HIER/bin/apt-via-ssh" "${O[@]}" -s -y "$ZIEL" upgrade
pruefe "install -s"           "^Inst |ist schon die neueste|already the newest" \
                                                       "$HIER/bin/apt-via-ssh" "${O[@]}" -s "$ZIEL" install sl
pruefe "run: Proxy-Umgebung"  "X-Apt-Via-Ssh"          "$HIER/bin/apt-via-ssh" "${O[@]}" "$ZIEL" run \
        'curl -sI http://deb.debian.org/debian/dists/stable/Release'
pruefe "offline-sync check"   "Paket\\(e\\)|aktuell"   "$HIER/bin/apt-offline-sync" -o BatchMode=yes "$ZIEL" check
echo "Ergebnis: $ok OK, $fehl fehlgeschlagen"
[ $fehl -eq 0 ]
