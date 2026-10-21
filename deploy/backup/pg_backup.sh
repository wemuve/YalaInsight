#!/bin/sh
# Nightly logical backup (keep 14 days). Complement with WAL archiving / managed PITR in production.
# Usage: PGHOST=... PGUSER=... PGPASSWORD=... BACKUP_DIR=/backups ./pg_backup.sh
set -eu
BACKUP_DIR=${BACKUP_DIR:-/backups}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR"
pg_dump --format=custom --no-owner --dbname="${PGDATABASE:-yalainsight}" --file="$BACKUP_DIR/yalainsight_$STAMP.dump"
# Encrypt at rest if a recipient key is configured
if [ -n "${BACKUP_GPG_RECIPIENT:-}" ]; then
  gpg --batch --yes --encrypt --recipient "$BACKUP_GPG_RECIPIENT" "$BACKUP_DIR/yalainsight_$STAMP.dump" && rm "$BACKUP_DIR/yalainsight_$STAMP.dump"
fi
find "$BACKUP_DIR" -name 'yalainsight_*' -mtime +14 -delete
echo "backup written: $STAMP"
