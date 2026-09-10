#!/usr/bin/env bash

set -Eeuo pipefail

# Creates a date-based release through the protected main branch:
# release branch -> PR -> required checks -> merge -> GitHub Release -> Docker publish.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYPROJECT_PATH="$PROJECT_ROOT/pyproject.toml"
REPOSITORY="${REPOSITORY:-planetic-labs/pulsar}"
WORKFLOW="docker-publish.yml"
MAX_RELEASES="${MAX_RELEASES:-10}"

cd "$PROJECT_ROOT"

for COMMAND in git gh uv; do
    if ! command -v "$COMMAND" >/dev/null 2>&1; then
        echo "Required command is not installed: $COMMAND" >&2
        exit 1
    fi
done

if [[ ! "$MAX_RELEASES" =~ ^[1-9][0-9]*$ ]]; then
    echo "MAX_RELEASES must be a positive integer." >&2
    exit 1
fi

if [ ! -f "$PYPROJECT_PATH" ]; then
    echo "pyproject.toml not found: $PYPROJECT_PATH" >&2
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    echo "The working tree is not clean. Commit or stash the changes before creating a release." >&2
    exit 1
fi

if [ "$(git branch --show-current)" != "main" ]; then
    echo "Switch to the main branch before creating a release." >&2
    exit 1
fi

gh auth status >/dev/null
git fetch origin main --tags --force
git pull --ff-only origin main

DATE_TAG="$(date -u +'%Y.%m.%d')"
LAST_TAG_TODAY="$(git tag -l "v${DATE_TAG}" "v${DATE_TAG}-patch*" | sort -V | tail -n 1)"

if [ -z "$LAST_TAG_TODAY" ]; then
    VERSION="v${DATE_TAG}"
elif [[ "$LAST_TAG_TODAY" =~ ^v${DATE_TAG}-patch([0-9]+)$ ]]; then
    VERSION="v${DATE_TAG}-patch$((BASH_REMATCH[1] + 1))"
else
    VERSION="v${DATE_TAG}-patch1"
fi

VERSION_NUM="${VERSION#v}"
VERSION_NUM="${VERSION_NUM//-patch/.}"
RELEASE_BRANCH="release/${VERSION}"

if git show-ref --verify --quiet "refs/heads/${RELEASE_BRANCH}" || \
    git show-ref --verify --quiet "refs/remotes/origin/${RELEASE_BRANCH}"; then
    echo "Release branch already exists: $RELEASE_BRANCH" >&2
    exit 1
fi

echo "Preparing $VERSION using branch $RELEASE_BRANCH"
git switch -c "$RELEASE_BRANCH"

sed -i 's/^version = "[^"]*"/version = "'"$VERSION_NUM"'"/' "$PYPROJECT_PATH"
uv lock

git add "$PYPROJECT_PATH" "$PROJECT_ROOT/uv.lock"
if git diff --cached --quiet; then
    echo "The project version is already $VERSION_NUM; nothing to release." >&2
    exit 1
fi

git commit -m "chore: bump version to $VERSION_NUM"
git push --set-upstream origin "$RELEASE_BRANCH"

PR_URL="$(gh pr create \
    --repo "$REPOSITORY" \
    --base main \
    --head "$RELEASE_BRANCH" \
    --title "chore: bump version to $VERSION_NUM" \
    --body "Prepare release $VERSION.")"
PR_NUMBER="${PR_URL##*/}"

echo "Created PR #$PR_NUMBER: $PR_URL"
gh pr merge "$PR_NUMBER" --repo "$REPOSITORY" --auto --squash --delete-branch

CHECK_COUNT=0
for _ in {1..12}; do
    CHECK_COUNT="$(gh pr view "$PR_NUMBER" --repo "$REPOSITORY" --json statusCheckRollup --jq '.statusCheckRollup | length')"
    if [ "$CHECK_COUNT" -gt 0 ]; then
        break
    fi
    sleep 5
done

if [ "$CHECK_COUNT" -eq 0 ]; then
    echo "No checks were registered for release PR #$PR_NUMBER." >&2
    exit 1
fi

echo "Waiting for required checks..."
if ! gh pr checks "$PR_NUMBER" --repo "$REPOSITORY" --watch --required >/dev/null; then
    echo "Required checks failed. See $PR_URL/checks" >&2
    exit 1
fi
echo "Required checks passed."

while [ "$(gh pr view "$PR_NUMBER" --repo "$REPOSITORY" --json state --jq .state)" = "OPEN" ]; do
    echo "Waiting for automatic merge of PR #$PR_NUMBER..."
    sleep 5
done

PR_STATE="$(gh pr view "$PR_NUMBER" --repo "$REPOSITORY" --json state --jq .state)"
if [ "$PR_STATE" != "MERGED" ]; then
    echo "Release PR #$PR_NUMBER finished with state $PR_STATE instead of MERGED." >&2
    exit 1
fi

git switch main
git pull --ff-only origin main

RELEASE_URL="$(gh release create "$VERSION" \
    --repo "$REPOSITORY" \
    --target main \
    --title "$VERSION" \
    --generate-notes)"

echo "Created release: $RELEASE_URL"

RUN_ID=""
for _ in {1..12}; do
    RUN_ID="$(gh run list \
        --repo "$REPOSITORY" \
        --workflow "$WORKFLOW" \
        --event release \
        --limit 10 \
        --json databaseId,displayTitle \
        --jq '.[] | select(.displayTitle == "'"$VERSION"'") | .databaseId' | head -n 1)"
    if [ -n "$RUN_ID" ]; then
        break
    fi
    sleep 5
done

if [ -z "$RUN_ID" ]; then
    echo "Release created, but the Docker publication run was not found." >&2
    exit 1
fi

echo "Watching Docker publication run $RUN_ID..."
gh run watch "$RUN_ID" --repo "$REPOSITORY" --exit-status
echo "Release $VERSION and its Docker image were published successfully."

echo "Cleaning up old releases; keeping the newest $MAX_RELEASES..."
mapfile -t OLD_RELEASES < <(
    gh release list \
        --repo "$REPOSITORY" \
        --limit 100 \
        --json tagName \
        --jq ".[${MAX_RELEASES}:] | .[].tagName"
)

for OLD_TAG in "${OLD_RELEASES[@]}"; do
    echo "Deleting old release and tag: $OLD_TAG"
    gh release delete "$OLD_TAG" --repo "$REPOSITORY" --yes --cleanup-tag
done

TODAY_DATE="$(date -u +'%Y-%m-%d')"
echo "Cleaning up workflow runs created before $TODAY_DATE..."
mapfile -t OLD_RUNS < <(
    gh run list \
        --repo "$REPOSITORY" \
        --limit 1000 \
        --created "<$TODAY_DATE" \
        --json databaseId \
        --jq '.[].databaseId'
)

for OLD_RUN_ID in "${OLD_RUNS[@]}"; do
    echo "Deleting old workflow run: $OLD_RUN_ID"
    gh run delete "$OLD_RUN_ID" --repo "$REPOSITORY"
done

echo "Release cleanup completed."
