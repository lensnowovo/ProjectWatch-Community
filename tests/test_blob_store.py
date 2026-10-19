import hashlib
import io
from pathlib import Path

import pytest

from project_watch.blob_store import BlobStore, UploadError


def test_store_uses_calculated_content_hash(tmp_path: Path) -> None:
    payload = b"Synthetic revenue table\n2025,120.5"
    store = BlobStore(tmp_path / "objects")

    result = store.put_stream("finance.txt", io.BytesIO(payload))

    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.display_name == "finance.txt"
    assert result.suffix == ".txt"
    assert result.size == len(payload)
    assert result.path.read_bytes() == payload
    assert result.path.is_relative_to(tmp_path / "objects")


def test_duplicate_bytes_keep_one_object(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "objects")
    first = store.put_stream("original.txt", io.BytesIO(b"synthetic"))
    second = store.put_stream("renamed.txt", io.BytesIO(b"synthetic"))

    assert second.path == first.path
    assert second.display_name == "renamed.txt"
    assert list((tmp_path / "objects").rglob(first.sha256)) == [first.path]


def test_submitted_path_is_never_used_as_storage_path(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "objects")

    result = store.put_stream(r"..\private\example.txt", io.BytesIO(b"synthetic"))

    assert result.display_name == "example.txt"
    assert result.path.is_relative_to(tmp_path / "objects")
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("filename", ["", "file.exe", "file.docm"])
def test_invalid_filename_or_suffix_rejected(tmp_path: Path, filename: str) -> None:
    store = BlobStore(tmp_path / "objects")

    with pytest.raises(UploadError):
        store.put_stream(filename, io.BytesIO(b"synthetic"))


def test_empty_or_oversize_upload_rejected(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "objects", max_bytes=8)

    with pytest.raises(UploadError, match="empty"):
        store.put_stream("empty.txt", io.BytesIO(b""))
    with pytest.raises(UploadError, match="size"):
        store.put_stream("large.txt", io.BytesIO(b"123456789"))

    assert list((tmp_path / "objects").rglob(".incoming-*")) == []


def test_inventory_detects_missing_corrupt_and_orphan_without_deleting(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "objects")
    referenced = store.put_stream("owned.txt", io.BytesIO(b"synthetic owned"))
    orphan = store.put_stream("orphan.txt", io.BytesIO(b"synthetic orphan"))
    missing_digest = hashlib.sha256(b"not uploaded").hexdigest()
    before = store.audit((referenced.sha256, missing_digest))
    assert before.present == (referenced.sha256,)
    assert before.missing == (missing_digest,)
    assert before.orphan == (orphan.sha256,)

    referenced.path.write_bytes(b"tampered")
    after = store.audit((referenced.sha256, missing_digest))
    assert after.corrupt == (referenced.sha256,)
    assert after.orphan == (orphan.sha256,)
    assert referenced.path.read_bytes() == b"tampered"
    assert orphan.path.read_bytes() == b"synthetic orphan"


def test_object_store_rejects_symlink_destination(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "objects")
    payload = b"synthetic"
    digest = hashlib.sha256(payload).hexdigest()
    parent = store.path_for_hash(digest).parent
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        parent.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable on this host")

    with pytest.raises(UploadError, match="symbolic link"):
        store.put_stream("source.txt", io.BytesIO(payload))
    assert not (target / digest).exists()
    assert store.audit((digest,)).unexpected == (digest[:2],)
