# Releasing CodeKB

CodeKB uses one [Semantic Versioning](https://semver.org/spec/v2.0.0.html) number
for the whole repository. The backend, frontend, and infrastructure all ship under
the same `MAJOR.MINOR.PATCH` version.

- **MAJOR** — a breaking change to the API, the space/index format, or the
  deployment/config contract (for example, `.env` keys or S3 state layout).
- **MINOR** — a new, backward-compatible feature.
- **PATCH** — backward-compatible fixes only.

The git tag `vX.Y.Z` and its matching GitHub Release are the authoritative record
of a release.

## Cutting a release

1. Pick the new version `X.Y.Z` based on what changed since the last tag.
2. In `CHANGELOG.md`, rename the `## [Unreleased]` section to
   `## [X.Y.Z] - YYYY-MM-DD`, remove any empty subheadings, then add a fresh empty
   `## [Unreleased]` section above it. Update the compare/tag links at the bottom.
3. Commit the changelog on `main`, for example: `chore(release): v X.Y.Z`.
4. Tag and push:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. Create the GitHub Release for the `vX.Y.Z` tag, pasting that version's
   `CHANGELOG.md` section as the release notes.

The README version badge reads the latest GitHub Release automatically, so there is
no version number to edit by hand. The `version` fields in `backend/pyproject.toml`
and `frontend/package.json` are not release versions and are left untouched — the
git tag is the source of truth.

## Keeping the changelog current

Add a bullet under `## [Unreleased]` in the same PR as the change, grouped under
Added / Changed / Fixed. That way the release step is just renaming the section.
