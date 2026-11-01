"""Shared fixtures for unit tests: an in-memory stand-in for the Blob SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from lib import blob_archive
from vercel.blob import sync as vercel_blob


@dataclass
class _Result:
    body: bytes


class FakeBlob:
    """In-memory replacement for ``vercel.blob.sync`` (no network)."""

    BlobError = vercel_blob.BlobError
    BlobNotFoundError = vercel_blob.BlobNotFoundError

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.puts: list[dict[str, Any]] = []
        self.heads: list[str] = []
        self.fail_puts: dict[str, list[Exception]] = {}
        self.fail_get: Exception | None = None
        self.fail_all_puts: Exception | None = None

    def put_blob(self, pathname: str, body: bytes, **kwargs: Any) -> None:
        self.puts.append({"pathname": pathname, **kwargs})
        if self.fail_all_puts is not None:
            raise self.fail_all_puts
        if self.fail_puts.get(pathname):
            raise self.fail_puts[pathname].pop(0)
        if pathname in self.files and not kwargs.get("allow_overwrite"):
            raise vercel_blob.BlobError("already exists", status_code=400)
        self.files[pathname] = bytes(body)

    def head_blob(self, pathname: str) -> None:
        self.heads.append(pathname)
        if pathname not in self.files:
            raise vercel_blob.BlobNotFoundError

    def get_blob(self, pathname: str, *, access: str) -> _Result:
        assert access == "private"
        if self.fail_get is not None:
            raise self.fail_get
        if pathname not in self.files:
            raise vercel_blob.BlobNotFoundError
        return _Result(self.files[pathname])


@pytest.fixture
def fake_blob(monkeypatch: pytest.MonkeyPatch) -> FakeBlob:
    """Patch the SDK used by ``lib.blob_archive`` and disable retry sleeps."""
    fake = FakeBlob()
    monkeypatch.setattr(blob_archive.vercel_blob, "put_blob", fake.put_blob)
    monkeypatch.setattr(blob_archive.vercel_blob, "head_blob", fake.head_blob)
    monkeypatch.setattr(blob_archive.vercel_blob, "get_blob", fake.get_blob)
    monkeypatch.setattr(blob_archive.time, "sleep", lambda _: None)
    return fake
