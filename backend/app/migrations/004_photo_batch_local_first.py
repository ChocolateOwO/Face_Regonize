"""Add Phase A state fields without changing historical columns or outputs."""
import sqlite3


COLUMNS = {
    'source_type': "TEXT NOT NULL DEFAULT 'drive'",
    'source_status': "TEXT NOT NULL DEFAULT 'PENDING'",
    'local_status': "TEXT NOT NULL DEFAULT 'CREATED'",
    'drive_status': "TEXT NOT NULL DEFAULT 'NOT_UPLOADED'",
    'workflow_version': 'INTEGER NOT NULL DEFAULT 1',
}


def upgrade(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute('PRAGMA table_info(photobatch)')}
    if not existing:
        return
    added = set()
    for name, ddl in COLUMNS.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE photobatch ADD COLUMN {name} {ddl}')
            added.add(name)
    # Only populate newly introduced columns. Re-running never changes live states.
    if 'source_status' in added:
        conn.execute("""UPDATE photobatch SET source_status = CASE
            WHEN status IN ('completed','syncing_drive') THEN 'READY'
            WHEN status='failed' THEN 'FAILED' ELSE 'PENDING' END""")
    if 'local_status' in added:
        conn.execute("""UPDATE photobatch SET local_status = CASE
            WHEN status IN ('completed','syncing_drive') THEN 'READY'
            WHEN status='processing' THEN 'PROCESSING'
            WHEN status='failed' THEN 'FAILED' ELSE 'CREATED' END""")
    if 'drive_status' in added:
        conn.execute("""UPDATE photobatch SET drive_status = CASE
            WHEN status='syncing_drive' THEN 'UPLOADING'
            WHEN drive_failed_photos > 0 OR COALESCE(drive_error,'') != '' THEN 'UPLOAD_FAILED'
            WHEN EXISTS (SELECT 1 FROM photobatchphoto p WHERE p.batch_id=photobatch.id
                         AND p.drive_upload_status='uploaded')
             AND NOT EXISTS (SELECT 1 FROM photobatchphoto p WHERE p.batch_id=photobatch.id
                             AND COALESCE(p.drive_upload_status,'pending') != 'uploaded')
            THEN 'UPLOADED' ELSE 'NOT_UPLOADED' END""")
