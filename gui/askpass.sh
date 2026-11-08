#!/bin/sh
# Wird von ssh ueber SSH_ASKPASS aufgerufen und gibt das passende Geheimnis aus.
# Rueckfragen anderer Art (z.B. "Hostschluessel annehmen?") werden nie
# automatisch beantwortet.
d="${APT_VIA_SSH_ASKPASS_DIR:-}"
[ -n "$d" ] || exit 1
case "${1:-}" in
    *assphrase*)                      f="$d/passphrase" ;;
    *assword*|*asswort*|*ennwort*)    f="$d/passwort" ;;
    *) exit 1 ;;
esac
[ -r "$f" ] || exit 1
cat "$f"
