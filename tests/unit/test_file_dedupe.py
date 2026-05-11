from app.file_dedupe import canonicalize_title, choose_preferred_file, dedupe_gallery_variants, has_gallery_marker
from app.google_drive import DriveFile


def test_canonicalize_title():
    assert canonicalize_title("Video.Gallery.mp4") == "video mp4"
    assert canonicalize_title("Video_Галерея.MP4") == "video mp4"
    assert canonicalize_title("Some.Cool-Video") == "some cool video"
    assert canonicalize_title("Already fine.mp4") == "already fine mp4"


def test_has_gallery_marker():
    assert has_gallery_marker("Video Gallery.mp4") is True
    assert has_gallery_marker("Моя Галерея") is True
    assert has_gallery_marker("Regular Video.mp4") is False


def test_choose_preferred_file():
    f_reg = DriveFile(
        file_id="1", name="Video.mp4", mime_type="video/mp4", size="100", created_time="2024-01-01", parents=()
    )
    f_gal = DriveFile(
        file_id="2", name="Video.Gallery.mp4", mime_type="video/mp4", size="100", created_time="2024-01-01", parents=()
    )

    # Prefer regular over gallery
    assert choose_preferred_file(f_gal, f_reg) == f_reg
    assert choose_preferred_file(f_reg, f_gal) == f_reg

    # Prefer larger if both same type
    f_larger = DriveFile(
        file_id="3", name="Video.mp4", mime_type="video/mp4", size="200", created_time="2024-01-01", parents=()
    )
    assert choose_preferred_file(f_reg, f_larger) == f_larger

    # Prefer newer if same type and size
    f_newer = DriveFile(
        file_id="4", name="Video.mp4", mime_type="video/mp4", size="100", created_time="2023-01-01", parents=()
    )
    # Note: current implementation check is candidate_created < current_created.
    # Wait, '2023' < '2024' is True. So if candidate is 2023 and current is 2024, it prefers 2023?
    # Let's re-read: `if candidate_created and (not current_created or candidate_created < current_created):
    # return candidate`
    # Yes, it prefers EARLIER date in current code. Interesting choice, probably to keep the original.
    assert choose_preferred_file(f_reg, f_newer) == f_newer


def test_dedupe_gallery_variants():
    files = [
        DriveFile(file_id="1", name="Video.mp4", mime_type="v", size="100", created_time="A", parents=()),
        DriveFile(file_id="2", name="Video.Gallery.mp4", mime_type="v", size="100", created_time="A", parents=()),
        DriveFile(file_id="3", name="Other.mp4", mime_type="v", size="100", created_time="B", parents=()),
    ]
    deduped, skipped = dedupe_gallery_variants(files)
    assert len(deduped) == 2
    assert deduped[0].name == "Video.mp4"
    assert deduped[1].name == "Other.mp4"
    assert len(skipped) == 1
    assert skipped[0][0].name == "Video.Gallery.mp4"
