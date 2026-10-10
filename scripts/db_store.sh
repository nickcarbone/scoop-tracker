#!/usr/bin/env bash
# Stores scoop_tracker.db as a GitHub Release asset instead of committing it to git.
#
# Why: git blocks any single file over 100 MiB, and the DB crossed that on
# 2026-10-06 (117k articles, ~1.3 MB/day growth). Release assets allow up to
# 2 GiB per file, with no total-size or bandwidth cap, and replacing one doesn't
# pile up history the way a commit does.
#
# Usage (needs GH_TOKEN and GITHUB_REPOSITORY, both provided in Actions):
#   scripts/db_store.sh pull   # download newest snapshot -> scoop_tracker.db
#   scripts/db_store.sh push   # upload scoop_tracker.db as a new snapshot, prune old ones
#
# Snapshots are named scoop_tracker-<UTC timestamp>.db.gz, so the newest is
# simply the last name in sort order. The newest KEEP snapshots are retained as
# rolling backups; nothing is deleted until the new upload is confirmed.
set -euo pipefail

DB="${DB:-scoop_tracker.db}"
TAG="${DB_STORE_TAG:-db-store}"
KEEP="${DB_STORE_KEEP:-8}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}"
ROWCOUNT_FILE=".db_rowcount_at_pull"

log() { echo "[db_store] $*"; }

article_count() {
  python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM articles').fetchone()[0])" "$1"
}

integrity_ok() {
  python3 -c "import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute('PRAGMA quick_check').fetchone()[0]; sys.exit(0 if r=='ok' else 1)" "$1"
}

# Prints "id<TAB>name" for every fully uploaded snapshot, oldest first.
list_snapshots() {
  gh api "repos/$REPO/releases/tags/$TAG" \
    --jq '.assets[] | select(.state=="uploaded") | select(.name|test("^scoop_tracker-.*\\.db\\.gz$")) | "\(.id)\t\(.name)"' \
    | sort -t$'\t' -k2
}

# One-time migration: the last scoop_tracker.db committed to git before the
# move to release storage. Used only if the release has no snapshots yet.
SEED_COMMIT="bd2ac15ffb584e5830c18bd6e7b2d1bcfd9fb0d9"

bootstrap_from_git() {
  echo "::warning::db_store: no snapshot on release '$TAG'; seeding from git commit $SEED_COMMIT (one-time migration)"
  if ! gh api "repos/$REPO/releases/tags/$TAG" --silent 2>/dev/null; then
    gh release create "$TAG" -R "$REPO" --target main --prerelease --latest=false \
      --title "Database storage (automated)" \
      --notes "Not a software release. Holds rolling snapshots of scoop_tracker.db, uploaded by the Scoop Tracker workflow after each run (scripts/db_store.sh). The newest $KEEP are kept. To analyse the data, download the newest .db.gz, unzip it, and open it with any SQLite tool."
  fi
  git fetch --depth=1 origin "$SEED_COMMIT"
  git show "$SEED_COMMIT:scoop_tracker.db" > "$DB"
}

pull() {
  local newest=""
  if gh api "repos/$REPO/releases/tags/$TAG" --silent 2>/dev/null; then
    newest="$(list_snapshots | tail -n1 | cut -f2)"
  fi
  rm -f "$DB" "$DB-wal" "$DB-shm"
  if [[ -z "$newest" ]]; then
    if [[ "${DB_STORE_NO_BOOTSTRAP:-}" == "1" ]]; then
      log "ERROR: no snapshot on release '$TAG' and bootstrap disabled for this job"
      exit 1
    fi
    bootstrap_from_git
  else
    log "downloading $newest"
    rm -f "$newest"
    gh release download "$TAG" -R "$REPO" -p "$newest"
    gunzip -c "$newest" > "$DB"
    rm -f "$newest"
  fi
  integrity_ok "$DB" || { log "ERROR: downloaded snapshot failed integrity check"; exit 1; }
  local n
  n="$(article_count "$DB")"
  echo "$n" > "$ROWCOUNT_FILE"
  log "restored $DB ($n articles, $(du -h "$DB" | cut -f1))"
}

push() {
  [[ -f "$DB" ]] || { log "ERROR: $DB missing, nothing to upload"; exit 1; }
  # Fold any WAL contents into the main file so the snapshot is self-contained.
  python3 -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" "$DB"
  integrity_ok "$DB" || { log "ERROR: $DB failed integrity check, not uploading"; exit 1; }

  local n before
  n="$(article_count "$DB")"
  if [[ -f "$ROWCOUNT_FILE" ]]; then
    before="$(cat "$ROWCOUNT_FILE")"
    # Articles are insert-only, so a shrinking count means something went wrong
    # (e.g. a fresh empty DB got created). Never let that overwrite good history.
    if (( n < before )); then
      log "ERROR: article count fell from $before to $n, refusing to upload"
      exit 1
    fi
  else
    log "ERROR: no $ROWCOUNT_FILE; push must follow a successful pull in the same job"
    exit 1
  fi

  local name="scoop_tracker-$(date -u +%Y%m%dT%H%M%SZ).db.gz"
  gzip -c -6 "$DB" > "$name"
  log "uploading $name ($n articles, $(du -h "$name" | cut -f1) compressed)"
  gh release upload "$TAG" -R "$REPO" "$name"

  # Confirm the upload landed before deleting anything.
  if ! list_snapshots | cut -f2 | grep -qx "$name"; then
    log "ERROR: $name not visible on release after upload; skipping prune"
    exit 1
  fi
  rm -f "$name"

  local total
  total="$(list_snapshots | wc -l)"
  if (( total > KEEP )); then
    list_snapshots | head -n $(( total - KEEP )) | while IFS=$'\t' read -r id old; do
      log "pruning old snapshot $old"
      gh api -X DELETE "repos/$REPO/releases/assets/$id" --silent
    done
  fi
  log "done; keeping newest $KEEP snapshots"
}

case "${1:-}" in
  pull) pull ;;
  push) push ;;
  *) echo "usage: $0 pull|push" >&2; exit 2 ;;
esac
