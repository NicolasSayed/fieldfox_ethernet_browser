"""SCPI file queries over TCP, using documented FieldFox commands only."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
import csv
import hashlib
import math
import os
import re
import socket
import tempfile
import threading

PORT = 5025
MAX_CATALOG_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
DRIVES = ("INTERNAL", "SDCARD", "USBDISK", "USBDISK1", "USBDISK2")


class BrowserError(ValueError):
    """Unsupported response, invalid selection or inconsistent transfer."""


class Cancelled(BrowserError):
    """The user cancelled an operation."""


def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("Operation cancelled")


def component(name: str, *, local: bool = False) -> str:
    if not name or name in {".", ".."} or any(c in name for c in '\\/:"') or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise BrowserError(f"Not a safe single file/folder name: {name!r}")
    if local and (any(c in name for c in '<>|?*') or name.endswith((" ", ".")) or
                  re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I)):
        raise BrowserError(f"Name is not portable to a Windows download folder: {name!r}")
    return name


@dataclass(frozen=True)
class RemotePath:
    drive: str
    parts: tuple[str, ...] = ()
    brackets: bool = True

    def __post_init__(self):
        if self.drive not in DRIVES:
            raise BrowserError(f"Unsupported storage alias {self.drive!r}")
        for name in self.parts:
            component(name)

    @classmethod
    def parse(cls, value: str | RemotePath) -> RemotePath:
        if isinstance(value, cls):
            return value
        match = re.fullmatch(r"(?:\[([A-Za-z0-9]+)\]|([A-Za-z0-9]+)):(.*)", value.strip())
        if not match:
            raise BrowserError("Use an absolute FieldFox path such as [INTERNAL]:\\RecordPlayback\\GPSA; plain INTERNAL: paths are accepted too")
        parts = tuple(p for p in re.split(r"[\\/]", match[3]) if p)
        return cls((match[1] or match[2]).upper(), parts, match[1] is not None)

    def __str__(self) -> str:
        drive = f"[{self.drive}]" if self.brackets else self.drive
        return drive + ":\\" + "\\".join(self.parts)

    @property
    def parent(self) -> RemotePath:
        return RemotePath(self.drive, self.parts[:-1], self.brackets)

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else self.drive

    def child(self, name: str) -> RemotePath:
        return RemotePath(self.drive, self.parts + (component(name),), self.brackets)


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int | None
    file_type: str = ""


@dataclass(frozen=True)
class Catalog:
    used_bytes: int
    free_bytes: int
    entries: tuple[Entry, ...]
    raw: str


def _integer(text: str) -> int:
    if not re.fullmatch(r"\+?\d{1,20}", text.strip()) or int(text) > 2**64-1:
        raise BrowserError(f"Expected a nonnegative catalogue byte count, got {text!r}")
    return int(text)


def parse_catalog(text: str) -> Catalog:
    """Accept quoted file-listing triples and the flattened Data Link layout.

    Each entry carries name,type,size. Brackets around a name mark a folder;
    explicit DIR/FOLD type markers are accepted as well. Unknown types remain
    visible rather than being interpreted as recording formats.
    """
    try:
        fields = next(csv.reader([text], skipinitialspace=True, strict=True))
        if len(fields) < 2:
            raise BrowserError("Catalogue has no used/free header")
        used, free = _integer(fields[0]), _integer(fields[1])
        tail = fields[2:]
        if not tail or all(not v.strip() for v in tail):
            return Catalog(used, free, (), text)
        flat = len(tail) % 3 == 0 and all(not v.strip() or re.fullmatch(r"\+?\d+", v.strip()) for v in tail[2::3])
        triples = [tail[i:i+3] for i in range(0, len(tail), 3)] if flat else [v.rsplit(",", 2) for v in tail]
        entries = []
        seen = set()
        for triple in triples:
            if len(triple) != 3:
                raise BrowserError(f"Unrecognized catalogue entry: {triple!r}")
            name, kind, size_text = triple
            # Names inside quoted whole-entry strings may retain nested quotes.
            if name.startswith('"') and name.endswith('"'):
                name = next(csv.reader([name], strict=True))[0]
            directory = (name.startswith("[") and name.endswith("]")) or kind.strip().upper() in {"DIR", "FOLD", "FOLDER"}
            if name.startswith("[") and name.endswith("]"):
                name = name[1:-1]
            if directory and name in {".", ".."}:
                continue
            component(name)
            if name in seen:
                raise BrowserError(f"Duplicate catalogue name: {name!r}")
            seen.add(name)
            size = None if directory and not size_text.strip() else _integer(size_text)
            entries.append(Entry(name, directory, size, kind.strip()))
        return Catalog(used, free, tuple(sorted(entries, key=lambda e: (not e.is_dir, e.name.casefold()))), text)
    except (csv.Error, StopIteration) as error:
        raise BrowserError(f"Malformed catalogue response: {error}") from error


@contextmanager
def destination(path: str | Path) -> Iterator[tuple[Path, object]]:
    """Write to a temporary sibling and publish without replacing a file."""
    target = Path(path).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".fieldfox-", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            yield temp, stream
        if os.name == "nt":
            os.rename(temp, target)  # Windows refuses an existing destination.
        else:
            os.link(temp, target)    # Atomic no-overwrite publication on POSIX.
            temp.unlink()
    finally:
        temp.unlink(missing_ok=True)


class Connection:
    def __init__(self, host: str, port: int, timeout: float, cancel: threading.Event | None):
        self.host, self.port, self.timeout, self.cancel = host, port, timeout, cancel
        self.sock: socket.socket | None = None
        self.buffer = bytearray()

    def __enter__(self):
        check_cancel(self.cancel)
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        return self

    def __exit__(self, *exc):
        if self.sock is not None:
            self.sock.close()

    def _send(self, command: str):
        check_cancel(self.cancel)
        self.sock.sendall((command + "\n").encode("utf-8"))

    def _receive(self):
        check_cancel(self.cancel)
        data = self.sock.recv(CHUNK_BYTES)
        if not data:
            raise BrowserError("Instrument closed the connection before the response was complete")
        self.buffer.extend(data)

    def _exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            self._receive()
        data = bytes(self.buffer[:count]); del self.buffer[:count]
        return data

    def _line(self) -> bytes:
        while b"\n" not in self.buffer:
            if len(self.buffer) > MAX_CATALOG_BYTES:
                raise BrowserError("Text response exceeds the catalogue memory limit")
            self._receive()
        end = self.buffer.index(10)
        if end > MAX_CATALOG_BYTES:
            raise BrowserError("Text response exceeds the catalogue memory limit")
        line = bytes(self.buffer[:end]).rstrip(b"\r"); del self.buffer[:end+1]
        return line

    def text(self, command: str) -> str:
        self._send(command)
        try:
            return self._line().decode("utf-8")
        except UnicodeDecodeError as error:
            raise BrowserError("The catalogue/identity is not UTF-8; this firmware's filename encoding needs verification") from error

    def catalog(self, path: RemotePath) -> Catalog:
        return parse_catalog(self.text(f'MMEM:CAT? "{path}"'))

    def download(self, path: RemotePath, output: Path, *, expected_size: int | None,
                 max_bytes: int, progress: Callable[[str], None] | None = None) -> dict:
        self._send(f'MMEM:DATA? "{path}"')
        if self._exact(1) != b"#":
            raise BrowserError("Expected an IEEE 488.2 binary block; the path may be missing, inaccessible or not a file")
        digits = self._exact(1)
        if digits not in b"123456789":
            raise BrowserError("Unsupported binary block header; indefinite-length #0 blocks are not accepted")
        raw_size = self._exact(int(digits))
        if not raw_size.isdigit():
            raise BrowserError("Malformed binary block length")
        length = int(raw_size)
        if length > max_bytes:
            raise BrowserError(f"File advertises {length} bytes, exceeding the {max_bytes}-byte limit")
        if expected_size is not None and length != expected_size:
            raise BrowserError(f"Remote file size changed or differs from the catalogue: {expected_size} vs {length}")
        digest = hashlib.sha256(); remaining = length
        with destination(output) as (_, stream):
            while remaining:
                check_cancel(self.cancel)
                data = self._exact(min(CHUNK_BYTES, remaining))
                stream.write(data); digest.update(data); remaining -= len(data)
                if progress:
                    progress(f"{path.name}: {length-remaining:,} / {length:,} bytes")
            # TCP SCPI responses terminate with LF or CRLF. Consume it before
            # another query; a missing terminator fails rather than desynchronizes.
            ending = self._exact(1)
            if ending == b"\r":
                ending = self._exact(1)
            if ending != b"\n":
                raise BrowserError("Unexpected binary block terminator")
            check_cancel(self.cancel)
        return {"remote_path": str(path), "local_path": str(output), "bytes": length, "sha256": digest.hexdigest()}


class FieldFoxClient:
    """Query only the user-selected host. No discovery, mode changes or uploads."""

    def __init__(self, host: str, port: int = PORT, timeout: float = 10):
        if not host.strip() or any(c.isspace() for c in host) or "://" in host:
            raise BrowserError("Enter an instrument IP address or hostname without a URL scheme")
        if not 1 <= port <= 65535 or not math.isfinite(timeout) or timeout <= 0:
            raise BrowserError("Port must be 1..65535 and timeout must be a positive number")
        self.host, self.port, self.timeout = host.strip(), port, timeout

    def connection(self, cancel: threading.Event | None = None) -> Connection:
        return Connection(self.host, self.port, self.timeout, cancel)

    def identify(self) -> str:
        with self.connection() as connection:
            return connection.text("*IDN?")

    def browse(self, path: str | RemotePath, cancel: threading.Event | None = None) -> tuple[str, Catalog]:
        path = RemotePath.parse(path)
        with self.connection(cancel) as connection:
            identity = connection.text("*IDN?")
            return identity, connection.catalog(path)

    def catalog(self, path: str | RemotePath, cancel: threading.Event | None = None) -> Catalog:
        path = RemotePath.parse(path)
        with self.connection(cancel) as connection:
            return connection.catalog(path)

    def download_file(self, remote: str | RemotePath, output: str | Path, *,
                      expected_size: int | None = None, max_bytes: int = 1024**3,
                      cancel: threading.Event | None = None,
                      progress: Callable[[str], None] | None = None) -> dict:
        path = RemotePath.parse(remote)
        if not path.parts:
            raise BrowserError("Select a file rather than a storage root")
        if max_bytes < 0:
            raise BrowserError("max_bytes must be nonnegative")
        target = Path(output).expanduser().absolute()
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Destination already exists: {target}")
        with self.connection(cancel) as connection:
            return connection.download(path, target, expected_size=expected_size, max_bytes=max_bytes, progress=progress)

    def download_folder(self, remote: str | RemotePath, output: str | Path, **options) -> dict:
        from .download import download_folder
        return download_folder(self, RemotePath.parse(remote), Path(output), **options)
