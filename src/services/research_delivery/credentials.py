"""Immutable, write-only credentials. Importing this module performs no I/O."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid


class CredentialError(Exception):
    def __init__(self):
        super().__init__("Credential validation or protected storage unavailable")


def validate_webhook_url(value: str) -> str:
    """Require one canonical official URL for hashing, storage and transport.

    Match the raw URL instead of a normalized parser result: scheme casing,
    ignored control characters and empty delimiters must not create alternate
    fingerprints for the same actual target. Transport must disable redirects.
    """
    if not isinstance(value, str) or not re.fullmatch(
        r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9_-]{1,200}", value
    ):
        raise CredentialError()
    return value


def credential_fingerprint(webhook_url: str) -> str:
    return hashlib.sha256(validate_webhook_url(webhook_url).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Credentials:
    webhook_url: str = field(repr=False)
    signing_secret: str = field(default="", repr=False)


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Per-user Windows DPAPI, without any plaintext fallback or interactive UI."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    operation = library.CryptUnprotectData if decrypt else library.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise CredentialError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


class CredentialVault:
    """Entries are opaque immutable references, stored outside the database.

    Windows entries are DPAPI-protected for this service user. POSIX entries
    require owner-only directory/files. Moving users requires credential re-entry.
    No secret, URL or underlying exception appears in errors.
    """
    def __init__(self, root: Path):
        self.root = Path(root).absolute()

    def _directory(self):
        if self.root.is_symlink():
            raise CredentialError()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise CredentialError()
        if os.name != "nt":
            info = self.root.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise CredentialError()

    def _protect(self, data: bytes) -> bytes:
        return b"DPAPI1\n" + _dpapi(data) if os.name == "nt" else b"POSIX1\n" + data

    def _unprotect(self, data: bytes) -> bytes:
        if os.name == "nt" and data.startswith(b"DPAPI1\n"):
            return _dpapi(data[7:], decrypt=True)
        if os.name != "nt" and data.startswith(b"POSIX1\n"):
            return data[7:]
        raise CredentialError()

    def store(self, webhook_url: str, signing_secret: str = "") -> str:
        validate_webhook_url(webhook_url)
        if not isinstance(signing_secret, str) or len(signing_secret) > 512:
            raise CredentialError()
        temporary = None
        try:
            self._directory()
            raw = json.dumps({"webhook_url": webhook_url, "signing_secret": signing_secret}).encode("utf-8")
            protected = self._protect(raw)
            reference = uuid.uuid4().hex
            fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            with os.fdopen(fd, "wb") as stream:
                stream.write(protected)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-link publication is atomic and never overwrites an existing entry.
            os.link(temporary, self.root / reference)
            return reference
        except Exception:
            raise CredentialError() from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def resolve(self, reference: str) -> Credentials:
        if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f]{32}", reference):
            raise CredentialError()
        try:
            self._directory()
            path = self.root / reference
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise CredentialError()
            if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise CredentialError()
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise CredentialError()
            data = json.loads(self._unprotect(raw))
            validate_webhook_url(data["webhook_url"])
            if not isinstance(data["signing_secret"], str) or len(data["signing_secret"]) > 512:
                raise CredentialError()
            return Credentials(**data)
        except Exception:
            raise CredentialError() from None
