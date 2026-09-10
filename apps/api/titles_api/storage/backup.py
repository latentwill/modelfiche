from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from ..platform_io import fsync_directory as _fsync_directory
from pathlib import Path
import sqlite3
import struct
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_MAGIC = b"TITLES-AEAD-1\n"
_CHUNK_SIZE = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    backup_id: str
    kind: str
    envelope_path: Path
    sha256: str
    size: int
    bindings: dict[str, object]


class FileKeyStore:
    """Owner-only key store used by tests and non-keychain installations."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def create(self, key_id: str) -> bytes:
        key = os.urandom(32)
        path = self._path(key_id)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(self.root)
        return key

    def get(self, key_id: str) -> bytes:
        path = self._path(key_id)
        data = path.read_bytes()
        if len(data) != 32 or path.stat().st_mode & 0o077:
            raise ValueError("invalid backup key")
        return data

    def delete(self, key_id: str) -> None:
        self._path(key_id).unlink(missing_ok=True)
        _fsync_directory(self.root)

    def _path(self, key_id: str) -> Path:
        try:
            parsed = uuid.UUID(key_id)
        except ValueError as exc:
            raise ValueError("invalid key id") from exc
        return self.root / f"{parsed}.key"


class EnvelopeStore:
    def __init__(self, root: Path, keys: FileKeyStore) -> None:
        self.root = root
        self.keys = keys
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def create(self, source_database: Path, *, kind: str, bindings: dict[str, object]) -> BackupReceipt:
        if kind not in {"raw", "sanitized"}:
            raise ValueError("backup kind must be raw or sanitized")
        backup_id = str(uuid.uuid4())
        plaintext = self.root / f"{backup_id}.plaintext"
        partial = self.root / f"{backup_id}.partial"
        destination = self.root / f"{backup_id}.envelope"
        key = self.keys.create(backup_id)
        try:
            self._sqlite_backup(source_database, plaintext)
            sha256, size = _hash_file(plaintext)
            nonce = os.urandom(12)
            header = {
                "backup_id": backup_id,
                "bindings": bindings,
                "cipher": "AES-256-GCM",
                "kind": kind,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "sha256": sha256,
                "size": size,
                "version": 1,
            }
            header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
            encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header_bytes)
            with plaintext.open("rb") as source, partial.open("xb") as output:
                output.write(_MAGIC)
                output.write(struct.pack(">Q", len(header_bytes)))
                output.write(header_bytes)
                while chunk := source.read(_CHUNK_SIZE):
                    output.write(encryptor.update(chunk))
                output.write(encryptor.finalize())
                output.write(encryptor.tag)
                output.flush()
                os.fsync(output.fileno())
            os.replace(partial, destination)
            _fsync_directory(self.root)
            self._read_header(destination)
            return BackupReceipt(backup_id, kind, destination, sha256, size, dict(bindings))
        except BaseException:
            partial.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            self.keys.delete(backup_id)
            raise
        finally:
            plaintext.unlink(missing_ok=True)
            _fsync_directory(self.root)

    def restore(self, backup_id: str, destination: Path) -> BackupReceipt:
        envelope = self.root / f"{uuid.UUID(backup_id)}.envelope"
        header, ciphertext_offset = self._read_header(envelope)
        key = self.keys.get(backup_id)
        nonce = base64.b64decode(str(header["nonce"]), validate=True)
        total_size = envelope.stat().st_size
        ciphertext_size = total_size - ciphertext_offset - 16
        if ciphertext_size < 0:
            raise ValueError("truncated backup envelope")
        with envelope.open("rb") as source:
            source.seek(total_size - 16)
            tag = source.read(16)
            source.seek(ciphertext_offset)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            header_bytes = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
            decryptor.authenticate_additional_data(header_bytes)
            temp = destination.with_name(f".{destination.name}.{backup_id}.restore")
            remaining = ciphertext_size
            try:
                with temp.open("xb") as output:
                    while remaining:
                        chunk = source.read(min(_CHUNK_SIZE, remaining))
                        if not chunk:
                            raise ValueError("truncated backup ciphertext")
                        remaining -= len(chunk)
                        output.write(decryptor.update(chunk))
                    output.write(decryptor.finalize())
                    output.flush()
                    os.fsync(output.fileno())
                sha256, size = _hash_file(temp)
                if sha256 != header["sha256"] or size != header["size"]:
                    raise ValueError("backup plaintext integrity mismatch")
                _verify_sqlite(temp)
                os.replace(temp, destination)
                _fsync_directory(destination.parent)
            finally:
                temp.unlink(missing_ok=True)
        return BackupReceipt(
            backup_id=backup_id,
            kind=str(header["kind"]),
            envelope_path=envelope,
            sha256=str(header["sha256"]),
            size=int(header["size"]),
            bindings=dict(header["bindings"]),
        )

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db, sqlite3.connect(destination) as target_db:
            source_db.backup(target_db, pages=256)
        _verify_sqlite(destination)
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())

    @staticmethod
    def _read_header(path: Path) -> tuple[dict[str, object], int]:
        with path.open("rb") as source:
            if source.read(len(_MAGIC)) != _MAGIC:
                raise ValueError("invalid backup envelope")
            raw_length = source.read(8)
            if len(raw_length) != 8:
                raise ValueError("truncated backup header")
            header_length = struct.unpack(">Q", raw_length)[0]
            if header_length > 1024 * 1024:
                raise ValueError("backup header is too large")
            header_bytes = source.read(header_length)
            if len(header_bytes) != header_length:
                raise ValueError("truncated backup header")
            header = json.loads(header_bytes)
            required = {"backup_id", "bindings", "cipher", "kind", "nonce", "sha256", "size", "version"}
            if set(header) != required or header["cipher"] != "AES-256-GCM" or header["version"] != 1:
                raise ValueError("unsupported backup header")
            return header, len(_MAGIC) + 8 + header_length


def _verify_sqlite(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError("SQLite backup failed integrity check")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
