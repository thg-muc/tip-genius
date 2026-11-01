"""Shared fixtures for unit tests: an in-memory stand-in for the Blob SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import pytest
from lib import blob_archive
from vercel.blob import sync as vercel_blob

STORE_URL = "https://store123.private.blob.vercel-storage.com"
# Message the Blob API returns for a write-once put on an existing pathname
ALREADY_EXISTS = (
    "This blob already exists, use `allowOverwrite: true` if you want to overwrite it."
)


@dataclass
class _Result:
    body: bytes


@dataclass
class _Head:
    url: str
    etag: str


class FakeBlob:
    """In-memory replacement for ``vercel.blob.sync`` (no network)."""

    BlobError = vercel_blob.BlobError
    BlobNotFoundError = vercel_blob.BlobNotFoundError

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.puts: list[dict[str, Any]] = []
        self.heads: list[str] = []
        self.gets: list[str] = []
        self.fail_puts: dict[str, list[BaseException]] = {}
        self.fail_get: Exception | None = None
        self.fail_all_puts: Exception | None = None
        self.fail_heads: Exception | None = None
        self.versions: dict[str, int] = {}

    def put_blob(self, pathname: str, body: bytes, **kwargs: Any) -> None:
        self.puts.append({"pathname": pathname, **kwargs})
        if self.fail_all_puts is not None:
            raise self.fail_all_puts
        if self.fail_puts.get(pathname):
            raise self.fail_puts[pathname].pop(0)
        if pathname in self.files and not kwargs.get("allow_overwrite"):
            raise vercel_blob.BlobError(ALREADY_EXISTS, status_code=400)
        self.files[pathname] = bytes(body)
        self.versions[pathname] = self.versions.get(pathname, 0) + 1

    def head_blob(self, pathname: str) -> _Head:
        self.heads.append(pathname)
        if self.fail_heads is not None:
            raise self.fail_heads
        if pathname not in self.files:
            raise vercel_blob.BlobNotFoundError
        return _Head(f"{STORE_URL}/{pathname}", f'"v{self.versions.get(pathname, 1)}"')

    def get_blob(self, url_or_pathname: str, *, access: str) -> _Result:
        assert access == "private"
        self.gets.append(url_or_pathname)
        if self.fail_get is not None:
            raise self.fail_get
        pathname = url_or_pathname
        if url_or_pathname.startswith("https://"):
            pathname = urlsplit(url_or_pathname).path.removeprefix("/")
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
