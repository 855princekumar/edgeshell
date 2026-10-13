#!/bin/sh
# Let's Encrypt for EdgeShell. Idle until EDGESHELL_DOMAIN is set; then gets a
# certificate through nginx's ACME webroot and keeps it renewed. nginx notices
# the new files by itself (docker/nginx/entrypoint.sh).
#
# EDGESHELL_DOMAIN can be a hostname or the server's public IPv4 address. IP
# certificates are Let's Encrypt's short-lived profile (about 6 days), so they
# are renewed once a third of their lifetime is left; checks run every 6 hours
# and renewals happen only when due, well inside the rate limits.
set -u

domain() {
    case "${EDGESHELL_DOMAIN:-}" in
        ""|":80"|":443") echo "" ;;
        *) echo "$EDGESHELL_DOMAIN" | tr -cd 'A-Za-z0-9.-' ;;
    esac
}

is_ip() { echo "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; }

# Exit 0 when the certificate has less than a third of its lifetime left.
renewal_due() {
    python3 - "$1" <<'PY'
import sys, datetime
from cryptography import x509
cert = x509.load_pem_x509_certificate(open(sys.argv[1], 'rb').read())
start, end = cert.not_valid_before_utc, cert.not_valid_after_utc
left = end - datetime.datetime.now(datetime.timezone.utc)
sys.exit(0 if left < (end - start) / 3 else 1)
PY
}

while :; do
    d="$(domain)"
    if [ -z "$d" ]; then
        sleep 3600
        continue
    fi
    if [ -s "/etc/letsencrypt/live/$d/fullchain.pem" ]; then
        if renewal_due "/etc/letsencrypt/live/$d/fullchain.pem"; then
            echo "certbot: renewing the certificate for $d"
            certbot renew --cert-name "$d" --force-renewal --webroot -w /var/www/certbot --quiet \
                || echo "certbot: renewal failed; retrying in 6 hours" >&2
        fi
        sleep 21600
        continue
    fi
    if is_ip "$d"; then
        subject="--ip-address $d --preferred-profile shortlived --cert-name $d"
    else
        subject="-d $d --cert-name $d"
    fi
    case "${ACME_EMAIL:-}" in
        ""|*@example.com|*@example.org) contact="--register-unsafely-without-email" ;;
        *) contact="--email $ACME_EMAIL" ;;
    esac
    echo "certbot: requesting a certificate for $d"
    # shellcheck disable=SC2086
    if certbot certonly --webroot -w /var/www/certbot $subject $contact \
            --key-type ecdsa --elliptic-curve secp256r1 \
            --agree-tos --non-interactive --no-eff-email --keep-until-expiring; then
        echo "certbot: certificate for $d issued"
        sleep 43200
    else
        # DNS not pointing here yet, or port 80 closed. Retry slowly to stay
        # well inside Let's Encrypt's failed-validation limit.
        echo "certbot: could not get a certificate for $d; retrying in 20 minutes" >&2
        sleep 1200
    fi
done
