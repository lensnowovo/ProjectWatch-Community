#!/bin/sh
set -eu
CONTENTS=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUNTIME="$CONTENTS/Resources/runtime"
# No system Python/Node, bootstrap installer, shell expansion of user settings or telemetry.
unset PYTHONHOME PYTHONPATH
# PBS uses OpenSSL: retain an explicit user trust configuration, otherwise use bundled CAs.
if [ -z "${SSL_CERT_FILE:-}" ]; then
  export SSL_CERT_FILE="$RUNTIME/lib/python3.12/site-packages/certifi/cacert.pem"
fi
cd "$CONTENTS/Resources"
exec "$RUNTIME/bin/python3.12" -I -B -m project_watch.studio.desktop "$@"
