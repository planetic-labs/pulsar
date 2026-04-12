from __future__ import annotations

import re

from app.google_drive import DriveFile


GALLERY_PATTERN = re.compile(r"\b(gallery|галерея)\b", re.IGNORECASE)
WHITESPACE_PATTERN = re.compile(r"\s+")


def canonicalize_title(title: str) -> str:
    stem, dot, suffix = title.rpartition(".")
    base = stem if dot else title
    extension = suffix.lower() if dot else ""

    base = GALLERY_PATTERN.sub(" ", base)
    base = re.sub(r"[._\-]+", " ", base)
    base = WHITESPACE_PATTERN.sub(" ", base).strip().lower()

    if extension:
        return f"{base}.{extension}"
    return base


def has_gallery_marker(title: str) -> bool:
    return bool(GALLERY_PATTERN.search(title))


def choose_preferred_file(current: DriveFile, candidate: DriveFile) -> DriveFile:
    current_is_gallery = has_gallery_marker(current.name)
    candidate_is_gallery = has_gallery_marker(candidate.name)

    if current_is_gallery != candidate_is_gallery:
        return candidate if not candidate_is_gallery else current

    current_created = current.created_time or ""
    candidate_created = candidate.created_time or ""
    if candidate_created and (not current_created or candidate_created < current_created):
        return candidate

    current_size = int(current.size) if current.size and current.size.isdigit() else -1
    candidate_size = int(candidate.size) if candidate.size and candidate.size.isdigit() else -1
    if candidate_size > current_size:
        return candidate

    return current


def dedupe_gallery_variants(files: list[DriveFile]) -> tuple[list[DriveFile], list[tuple[DriveFile, DriveFile]]]:
    preferred_by_key: dict[str, DriveFile] = {}
    skipped_pairs: list[tuple[DriveFile, DriveFile]] = []

    for file_meta in files:
        key = canonicalize_title(file_meta.name)
        existing = preferred_by_key.get(key)
        if existing is None:
            preferred_by_key[key] = file_meta
            continue

        preferred = choose_preferred_file(existing, file_meta)
        skipped = existing if preferred is file_meta else file_meta
        preferred_by_key[key] = preferred
        skipped_pairs.append((skipped, preferred))

    deduped = sorted(
        preferred_by_key.values(),
        key=lambda item: (item.created_time or "", item.name.lower()),
    )
    return deduped, skipped_pairs
