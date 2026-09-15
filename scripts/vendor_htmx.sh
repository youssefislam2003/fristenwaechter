#!/usr/bin/env bash
# Re-vendor htmx (self-hosted; NO CDN at runtime — CSP is default-src 'self').
# Pins the version and verifies the SHA-256 so the vendored asset is
# reproducible and tamper-evident. Run from the repo root.
set -euo pipefail

HTMX_VERSION="2.0.4"
EXPECTED_SHA256="e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447"
URL="https://unpkg.com/htmx.org@${HTMX_VERSION}/dist/htmx.min.js"
DEST="app/web/static/htmx.min.js"

echo "Fetching htmx ${HTMX_VERSION} from ${URL}"
curl -fsSL "$URL" -o "$DEST"

actual="$(sha256sum "$DEST" | awk '{print $1}')"
if [[ "$actual" != "$EXPECTED_SHA256" ]]; then
  echo "SHA-256 mismatch!" >&2
  echo "  expected: $EXPECTED_SHA256" >&2
  echo "  actual:   $actual" >&2
  exit 1
fi
echo "OK: $DEST verified (sha256=$actual)"
