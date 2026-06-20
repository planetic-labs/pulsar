#!/bin/bash

# Скрипт для автоматического создания релиза в GitHub
# Формат версии: YYYY.MM.DD.REV (где REV - номер ревизии за день)

# 1. Генерируем версию на основе даты
DATE_TAG=$(date +'%Y.%m.%d')
# Проверяем, были ли уже теги сегодня
LAST_TAG_TODAY=$(git tag -l "v${DATE_TAG}*" | sort -V | tail -n 1)

if [ -z "$LAST_TAG_TODAY" ]; then
    VERSION="v${DATE_TAG}"
else
    # Если тег уже есть, проверяем есть ли уже патчи
    if [[ "$LAST_TAG_TODAY" == *"-patch"* ]]; then
        PATCH_NUM=$(echo $LAST_TAG_TODAY | awk -F"-patch" '{print $2}')
        NEXT_PATCH=$((PATCH_NUM + 1))
        VERSION="v${DATE_TAG}-patch${NEXT_PATCH}"
    else
        VERSION="v${DATE_TAG}-patch1"
    fi
fi

VERSION_NUM="${VERSION#v}"
# Заменяем "-patch" на "." для соответствия спецификации PEP 440 (валидная версия Python)
VERSION_NUM="${VERSION_NUM//-patch/.}"

# 1.1. Обновляем версию в pyproject.toml
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYPROJECT_PATH="$PROJECT_ROOT/pyproject.toml"

if [ -f "$PYPROJECT_PATH" ]; then
    sed -i 's/^version = "[^"]*"/version = "'"$VERSION_NUM"'"/' "$PYPROJECT_PATH"
    echo "Updated version in pyproject.toml to $VERSION_NUM"
else
    echo "❌ pyproject.toml not found!"
    exit 1
fi

# 1.2. Обновляем uv.lock и делаем коммит изменений версии
if command -v uv &> /dev/null; then
    (cd "$PROJECT_ROOT" && uv lock)
fi

git add "$PYPROJECT_PATH"
if [ -f "$PROJECT_ROOT/uv.lock" ]; then
    git add "$PROJECT_ROOT/uv.lock"
fi

if ! git diff --cached --quiet; then
    git commit -m "chore: bump version to $VERSION_NUM"
    echo "Pushed version bump to remote..."
    git push
else
    echo "No version changes to commit."
fi

echo "🚀 Preparing release $VERSION..."

# 2. Генерируем описание изменений (Changelog)
# Берем коммиты с момента последнего тега
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null)

if [ -z "$LAST_TAG" ]; then
    echo "First release detected."
    CHANGELOG=$(git log --pretty=format:"* %s (%h)")
else
    echo "Changes since $LAST_TAG:"
    CHANGELOG=$(git log ${LAST_TAG}..HEAD --pretty=format:"* %s (%h)")
fi

if [ -z "$CHANGELOG" ]; then
    CHANGELOG="Maintenance update and minor fixes."
fi

echo -e "📝 Changelog:\n$CHANGELOG"

# 3. Создаем релиз через GitHub CLI
# Передаем сгенерированный список коммитов напрямую в описание
gh release create "$VERSION" \
    --title "$VERSION" \
    --notes "$CHANGELOG"

if [ $? -eq 0 ]; then
    echo "✅ Release $VERSION successfully created!"
    
    # 4. Очистка старых релизов (держим только последние 10)
    MAX_RELEASES=10
    echo "🧹 Cleaning up old releases (keeping top $MAX_RELEASES)..."
    
    # Получаем список всех релизов, кроме последних 10
    OLD_RELEASES=$(gh release list --limit 100 | awk -v max=$MAX_RELEASES 'NR > max {print $1}')
    
    for OLD_TAG in $OLD_RELEASES; do
        echo "Deleting old release: $OLD_TAG"
        gh release delete "$OLD_TAG" --yes --cleanup-tag
    done
    # 5. Clean up old workflow runs (keep only runs created today)
    TODAY_DATE=$(date -u +'%Y-%m-%d')
    echo "🧹 Cleaning up workflow runs created before $TODAY_DATE..."
    OLD_RUNS=$(gh run list --limit 1000 --created "<$TODAY_DATE" --json databaseId --jq '.[].databaseId')
    if [ -n "$OLD_RUNS" ]; then
        RUN_COUNT=$(echo "$OLD_RUNS" | wc -l)
        echo "Found $RUN_COUNT older workflow runs. Deleting in parallel..."
        echo "$OLD_RUNS" | xargs -r -P 10 -I {} gh run delete {}
        echo "✅ Old workflow runs cleanup completed."
    else
        echo "No older workflow runs to clean up."
    fi

    echo "🔍 Track build progress: gh run watch"
else
    echo "❌ Failed to create release. Make sure you are logged in: gh auth login"
fi
