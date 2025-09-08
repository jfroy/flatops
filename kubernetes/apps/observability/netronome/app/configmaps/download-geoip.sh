#!/usr/bin/env sh
set -euo pipefail

# List of GeoLite2 database types to download
DATABASES="GeoLite2-ASN GeoLite2-Country"

# Download and extract each database
for DB in $DATABASES; do
    echo "⬇️ Downloading ${DB}..."
    curl -fsSL \
        -u "${username}:${password}" \
        "https://download.maxmind.com/geoip/databases/${DB}/download?suffix=tar.gz" \
        -o "/geoip/${DB}.tar.gz"
    tar --strip-components=1 -xzf "/geoip/${DB}.tar.gz" -C "/geoip"
    rm -f "/geoip/${DB}.tar.gz"
done
