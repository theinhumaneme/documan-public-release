#!/usr/bin/env bash
# Export the library's metadata so another database can adopt it.
#
# Why this exists rather than a second upload: the bytes are already in R2, and
# R2 is shared between environments — one bucket, one public host, whichever
# database is asking. Re-running the uploader against production would push the
# same 4.53 GB a second time under fresh random object names, orphaning every
# object the first run created. What production is actually missing is rows.
#
# So this moves rows. `file.object_name` and `file.object_url` already name live
# objects, and a row restored elsewhere points at the same bytes.
#
# Ids are carried across deliberately. subject, folder and file all reference
# each other by id, and department/year/semester are seeded from SQL/*.sql in a
# fixed order, so both databases number them identically — see role.sql for why
# insertion order is treated as load-bearing here. The reference tables are
# exported too so a mismatch shows up as a conflict on restore rather than as
# files quietly filed under the wrong department.
#
#   ./export_library_metadata.sh > library.sql
#
# Restore is in the header of the generated file.
set -euo pipefail

CONTAINER="${PG_CONTAINER:-postgres}"
DB="${PG_DB:-postgres}"
USER="${PG_USER:-root}"

TABLES=(department year semester subject folder file)

# --column-inserts, not COPY: the restore target may differ in column order, and
# an INSERT naming its columns survives that while a COPY stream does not.
# --data-only because the schema is Hibernate's to create, not this file's.
args=(--data-only --column-inserts --no-owner --no-privileges)
for t in "${TABLES[@]}"; do args+=(--table="$t"); done

{
  cat <<'HEADER'
-- Documan library metadata.
--
-- Rows only. The objects these rows name are already in R2 and are not touched
-- by this file.
--
-- Restore into a target whose schema exists but whose library is to be replaced:
--
--   psql -U <user> -d <db> -v ON_ERROR_STOP=1 -f library.sql
--
-- The truncate below is the "reset" half. It clears the library and the
-- bookmarks pointing into it, and leaves users, posts and comments alone.
-- Sequences are reset at the end, because rows inserted with explicit ids do
-- not advance them and the next upload would otherwise collide on id 1.
--
-- Search is not covered here. Meilisearch holds its own copy, so after
-- restoring, clear the files index and let the reconcile job repopulate it, or
-- the index will keep answering with the rows this file just deleted:
--
--   curl -X DELETE -H "Authorization: Bearer $MEILI_MASTER_KEY" \
--        "$MEILI_HOST/indexes/${MEILI_INDEX_PREFIX}files/documents"

BEGIN;

TRUNCATE TABLE favourite_file, file, folder, subject RESTART IDENTITY CASCADE;
DELETE FROM department;
DELETE FROM year;
DELETE FROM semester;

HEADER

  docker exec -i "$CONTAINER" pg_dump -U "$USER" -d "$DB" "${args[@]}"

  cat <<'FOOTER'

-- Explicit ids were inserted above, which leaves every sequence still at its
-- starting value. Without this the first row the application creates collides.
SELECT setval(pg_get_serial_sequence('department', 'id'), COALESCE((SELECT MAX(id) FROM department), 1));
SELECT setval(pg_get_serial_sequence('year', 'id'), COALESCE((SELECT MAX(id) FROM year), 1));
SELECT setval(pg_get_serial_sequence('semester', 'id'), COALESCE((SELECT MAX(id) FROM semester), 1));
SELECT setval(pg_get_serial_sequence('subject', 'id'), COALESCE((SELECT MAX(id) FROM subject), 1));
SELECT setval(pg_get_serial_sequence('folder', 'id'), COALESCE((SELECT MAX(id) FROM folder), 1));
SELECT setval(pg_get_serial_sequence('file', 'id'), COALESCE((SELECT MAX(id) FROM file), 1));

COMMIT;
FOOTER
}
