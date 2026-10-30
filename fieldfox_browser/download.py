"""Download a directory tree without exposing an incomplete final session."""
from __future__ import annotations

from pathlib import Path
from typing import Callable
import shutil
import tempfile
import threading

from .client import BrowserError, FieldFoxClient, RemotePath, check_cancel, component


def download_folder(client: FieldFoxClient, remote: RemotePath, output: Path, *,
                    max_entries: int = 100_000, max_depth: int = 32,
                    max_total_bytes: int = 100 * 1024**3,
                    max_file_bytes: int = 1024**3,
                    cancel: threading.Event | None = None,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Plan, copy, recheck listings and publish a new local folder.

    Names, sizes and directory membership must remain unchanged. Same-size
    remote content rewrites cannot be detected without a remote checksum;
    copy finished recordings. No metadata files are added to the copied tree.
    """
    if min(max_entries, max_depth) < 1 or min(max_total_bytes, max_file_bytes) < 0:
        raise BrowserError("Entry/depth limits must be positive and byte limits nonnegative")
    target = output.expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Destination already exists: {target}")
    plans, directories, snapshots = [], [], []
    stack = [(remote, Path(), 0)]
    entries_seen = total = 0
    with client.connection(cancel) as connection:
        if remote.parts:
            parent_listing = connection.catalog(remote.parent)
            if not any(e.name == remote.name and e.is_dir for e in parent_listing.entries):
                raise BrowserError(f"Remote folder is not listed as an accessible directory: {remote}")
        while stack:
            check_cancel(cancel)
            folder, relative, depth = stack.pop()
            if depth > max_depth:
                raise BrowserError("Folder nesting exceeds the depth limit")
            if progress:
                progress(f"Listing {folder}")
            catalog = connection.catalog(folder)
            snapshots.append((folder, catalog.entries))
            directories.append(relative)
            entries_seen += len(catalog.entries)
            if entries_seen > max_entries:
                raise BrowserError("Folder tree exceeds the entry limit")
            local_names = set()
            for entry in catalog.entries:
                name = component(entry.name, local=True)
                if name.casefold() in local_names:
                    raise BrowserError(f"Names collide on a Windows filesystem in {folder}: {name!r}")
                local_names.add(name.casefold())
                child, child_relative = folder.child(name), relative / name
                if entry.is_dir:
                    stack.append((child, child_relative, depth+1))
                else:
                    total += entry.size
                    if entry.size > max_file_bytes or total > max_total_bytes:
                        raise BrowserError("Download exceeds the configured file or total-byte limit")
                    plans.append((child, child_relative, entry.size))
        check_cancel(cancel)
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent.resolve()
        staging = Path(tempfile.mkdtemp(prefix=".fieldfox-", dir=parent)).resolve()
        # Verify every computed recursive cleanup/move target before using it.
        if staging.parent != parent or not staging.name.startswith(".fieldfox-"):
            raise BrowserError("Unexpected staging directory location")
        copied = []
        try:
            for relative in directories:
                path = staging / relative
                if not path.resolve().is_relative_to(staging):
                    raise BrowserError("Downloaded folder would escape the staging directory")
                path.mkdir(parents=True, exist_ok=True)
            for number, (path, relative, size) in enumerate(plans, 1):
                check_cancel(cancel)
                local = staging / relative
                if not local.resolve().is_relative_to(staging):
                    raise BrowserError("Downloaded file would escape the staging directory")
                if progress:
                    progress(f"File {number}/{len(plans)}: {path}")
                result = connection.download(path, local, expected_size=size,
                                             max_bytes=max_file_bytes, progress=progress)
                result["local_path"] = str(target / relative)
                copied.append(result)
            for folder, before in snapshots:
                check_cancel(cancel)
                if connection.catalog(folder).entries != before:
                    raise BrowserError("Remote folder changed during transfer; download a finished recording session")
            check_cancel(cancel)
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"Destination already exists: {target}")
            if target.parent.resolve() != parent or staging.parent != parent:
                raise BrowserError("Download destination changed during transfer")
            staging.rename(target)
        finally:
            if staging.exists():
                if staging.is_symlink() or staging.resolve().parent != parent or not staging.name.startswith(".fieldfox-"):
                    raise BrowserError("Refusing cleanup outside the verified staging directory")
                shutil.rmtree(staging)
    return {"destination": str(target), "files": copied, "file_count": len(copied), "bytes": total}
