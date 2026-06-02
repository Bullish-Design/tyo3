# Git — Version Tag

Create a git version tag for `{{ repo_name }}`.

## Input

The user will specify a bump level:
- `major` — breaking changes (X.0.0)
- `minor` — new features, backwards-compatible (0.X.0)
- `patch` — bug fixes, no new behaviour (0.0.X)

If no level is given, default to `patch`.

## Process

1. Determine the current version from the latest git tag:
   ```bash
   git describe --tags --abbrev=0 2>/dev/null || echo "0.0.0"
   ```
2. Parse the version into major.minor.patch
3. Increment the appropriate component (reset lower components to 0)
4. Create an annotated tag:
   ```bash
   git tag -a "v<new-version>" -m "<repo_name> v<new-version>"
   ```

## Tag message format

```
{{ repo_name }} v<version>

<brief summary of changes since last tag>
```

Generate the summary from `git log <previous-tag>..HEAD --oneline`.

## After tagging

- Confirm the tag exists: `git tag -l "v<new-version>"`
- Show the tag details: `git show "v<new-version>" --no-patch`
- Remind the user: `git push origin "v<new-version>"` (tags are not pushed by default)

## Rules

- Never delete or move existing tags
- Never tag if there are uncommitted changes (check `git status --porcelain`)
- Tag format: `v<major>.<minor>.<patch>` (semver with `v` prefix)
