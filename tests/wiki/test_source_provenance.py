from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path

import pytest

from wiki.source_provenance import SourceProvenanceError, SourceProvenanceResolver


def test_resolver_returns_relative_path_platform_key_hash_size_and_identity(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "raw" / "sources" / "file" / "project" / "note.md"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"raw evidence")

    resolved = SourceProvenanceResolver(root).resolve("raw\\sources\\file\\project\\note.md")

    assert resolved.relative_path == "raw/sources/file/project/note.md"
    assert resolved.path_key == os.path.normcase(os.fspath(Path("raw/sources/file/project/note.md")))
    assert resolved.sha256 == sha256(b"raw evidence").hexdigest()
    assert resolved.size_bytes == len(b"raw evidence")
    assert resolved.file_identity.size_bytes == len(b"raw evidence")


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("raw/assets/file.bin", "source_path_not_allowed"),
        ("raw/sources/missing.md", "source_not_found"),
        ("raw/sources/directory", "source_not_file"),
        ("../raw/sources/a.md", "path_escape"),
        ("/outside/raw/sources/a.md", "source_path_not_allowed"),
    ],
)
def test_resolver_maps_invalid_source_categories_to_stable_codes(tmp_path: Path, value: str, code: str) -> None:
    root = tmp_path / "vault"
    (root / "raw" / "sources").mkdir(parents=True)
    (root / "raw" / "sources" / "directory").mkdir()

    with pytest.raises(SourceProvenanceError) as error:
        SourceProvenanceResolver(root).resolve(value)

    assert error.value.code == code


def test_resolver_is_fail_closed_for_a_mixed_source_list(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    source = root / "raw" / "sources" / "a.md"
    source.parent.mkdir(parents=True)
    source.write_text("raw", encoding="utf-8")

    with pytest.raises(SourceProvenanceError) as error:
        SourceProvenanceResolver(root).resolve_many(["raw/sources/a.md", "wiki/a.md"])

    assert error.value.code == "source_path_not_allowed"
