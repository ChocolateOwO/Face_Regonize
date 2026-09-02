# patch/

Backups of live files taken immediately before they were modified or replaced,
per rule 7 in CLAUDE.md.

One folder per change:

```
patch/YYYY-MM-DD_HHMM_<key-change>/
    <original relative path of each replaced file>
    NOTES.md
```

The directory structure inside a patch folder mirrors the project, so restoring
is a straight copy back to the repo root.

These folders are history. Do not delete them as part of unrelated work.
