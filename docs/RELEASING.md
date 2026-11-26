# Publishing a Reconize release

**For:** the maintainer who publishes a release.

The web updater offers only a **published GitHub Release**. A pushed commit or a bare tag is never offered.

It installs a release only when that release carries a **`release-manifest.json`** asset whose SHA-256 checks out, and that names the exact commit and source tree of the tag.

## Steps

1. **Bump the version.** Set `VERSION` to the new version, e.g. `1.0.2`. Commit it on `main` with the change set it describes, and push.
2. **Tag the commit.** The tag must equal `v` + `VERSION`:

   ```powershell
   git tag -a v1.0.2 -m "Reconize 1.0.2"
   git push origin v1.0.2
   ```

3. **Build the integrity asset.** This reads the local repository only and publishes nothing:

   ```powershell
   backend\.venv\Scripts\python.exe tools\release\make_release_manifest.py v1.0.2
   ```

   It refuses when `VERSION` at the tag differs from the tag. It writes `dist-release\release-manifest.json` and `dist-release\release-manifest.json.sha256`; the `dist-release\` folder is git-ignored.

4. **Publish the release.** Use the GitHub web page or `gh`, attach both files, and do not create it as a draft:

   ```powershell
   gh release create v1.0.2 dist-release\release-manifest.json dist-release\release-manifest.json.sha256 --title "Reconize 1.0.2" --notes-file notes.md
   ```

   The release notes appear under "What's New" in Settings → Application Updates.

## What the updater checks

- **Before anything changes on the installed PC** (`backend/app/services/update_service.py`):
  - The release is published (not a draft).
  - `release-manifest.json` matches GitHub's SHA-256 digest of the uploaded asset, or the `.sha256` asset.
  - The manifest's tag, version and commit equal the tag's commit.
  - The working tree is clean.
  - No work is running.
  - The database backup is possible.
  - The admin confirmed this exact version.
- **After `git checkout`** (`tools/updater/updater.py`): HEAD, the source tree and `VERSION` must equal the manifest's.
- **After the restart:** `/api/health` must answer and `/api/update/version` must report the new version. Otherwise the previous commit and the verified database backup are restored.

The checksum detects corruption and a release whose manifest does not match its tag. It is not a signature: whoever can publish releases on the repository can publish a manifest too.

## Testing without a real release

`backend/tests/test_update_release.py` covers:

- the up-to-date state;
- a newer release being offered;
- a corrupt or mismatched manifest;
- a missing manifest or checksum;
- the version confirmation;
- refusal while work is running;
- the real updater process against a local git origin and a fake backend: a successful update, a failed start with rollback and database restore, and a tree mismatch.
