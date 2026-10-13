#!/bin/sh
# Let's Encrypt for EdgeShell. Idle until EDGESHELL_DOMAIN is set; then gets a
# certificate through nginx's ACME webroot and renews it twice a day. nginx
# notices the new files by itself (docker/nginx/entrypoint.sh).
set -u

domain() {
    case "${EDGESHELL_DOMAIN:-}" in
        ""|":80"|":443") echo "" ;;
        *) echo "$EDGESHELL_DOMAIN" | tr -cd 'A-Za-z0-9.-' ;;
    esac
}

while :; do
    d="$(domain)"
    if [ -z "$d" ]; then
        sleep 3600
        continue
    fi
    if [ -s "/etc/letsencrypt/live/$d/fullchain.pem" ]; then
        certbot renew --webroot -w /var/www/certbot --quiet || true
        sleep 43200
        continue
    fi
    case "${ACME_EMAIL:-}" in
        ""|*@example.com|*@example.org) contact="--register-unsafely-without-email" ;;
        *) contact="--email $ACME_EMAIL" ;;
    esac
    echo "certbot: requesting a certificate for $d"
    # shellcheck disable=SC2086
    if certbot certonly --webroot -w /var/www/certbot -d "$d" $contact \
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
