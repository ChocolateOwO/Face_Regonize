"""Versioned schema migrations.

SQLModel's create_all() creates missing TABLES but never adds columns to a
table that already exists, so a released schema change needs an explicit
migration to reach machines whose database already exists.

Scripts live beside this file, named NNN_description.py, each exposing
`upgrade(conn)` taking a raw sqlite3 connection. They run in filename order
and are recorded in the SchemaMigration table so each applies once.

Every script must be IDEMPOTENT — check before you alter. Some installations
(including the developer's own) were patched by hand before this runner
existed, so a migration can legitimately find its work already done.
"""
