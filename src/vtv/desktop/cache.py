"""The assets a job needs, on local disk, verified.

## Why the digest is not belt and braces

A truncated download is the single most likely thing to go wrong on a domestic
connection, and it does not raise. A half-written JPEG decodes — to the top two
thirds of a photograph and a grey band across the bottom. That band would be
composited, encoded, uploaded and delivered, and nothing between here and the
customer would notice, because every layer downstream is doing exactly what it
was asked to.

So bytes are hashed before they are usable, and a mismatch is a re-download
rather than a render. The digest arrives in the assignment, computed
server-side, from the same object the cloud renderer would have used.

## Why it is content-addressed

Files are named by their digest, not by their manifest key. A machine that
renders the same project twice, or two jobs that share a photograph, downloads
it once — and there is no cache-invalidation question at all, because a
different digest is a different file. The manifest key changes between jobs;
the bytes do not.

## Why it is bounded

An executor left running for a month on a machine somebody also uses would
otherwise fill their disk quietly, which is the sort of thing that ends with the
software being uninstalled. `sweep` keeps the directory under a budget by
discarding what was used longest ago, which for this workload is a good enough
policy and is the one a person would guess.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from vtv.contracts.devices import AssetHandout
from vtv.contracts.errors import ValidationFailed

#: Default ceiling for the cache directory. Generous enough that a day's work
#: never re-downloads, small enough to be an unremarkable amount of a disk.
DEFAULT_BUDGET_BYTES = 8 * 1024 * 1024 * 1024

#: Read size for hashing and copying. Large enough that the syscall overhead
#: disappears, small enough that a 2 GB video never becomes 2 GB of RAM.
CHUNK = 1024 * 1024


def digest_of(path: Path) -> str:
    """SHA-256 of a file on disk, streamed."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass
class AssetCache:
    """Content-addressed storage for the files jobs are drawn from."""

    root: Path
    budget_bytes: int = DEFAULT_BUDGET_BYTES

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        """Where a given digest lives.

        Two levels of fan-out, because a flat directory of a hundred thousand
        files is slow to list on every filesystem and pathological on some.
        """
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise ValidationFailed("an asset digest must be 64 hex characters")
        return self.root / sha256[:2] / sha256[2:4] / sha256

    def holds(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def store(self, handout: AssetHandout, source: Path) -> Path:
        """Take a downloaded file into the cache, or refuse it.

        Verified *before* it is moved into place, so a file that is present in
        the cache is a file that has been checked. The alternative — move, then
        verify, then delete on mismatch — leaves a window in which a concurrent
        reader sees a corrupt file that is about to be removed.
        """
        actual = digest_of(source)
        if actual != handout.sha256:
            raise ValidationFailed(
                f"{handout.key} arrived corrupt: expected {handout.sha256[:12]}, "
                f"got {actual[:12]}"
            )
        target = self.path_for(handout.sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        # `os.replace` rather than `shutil.move`: atomic within a filesystem, so
        # a reader either sees the whole file or does not see it at all. The
        # fallback covers a downloads directory on a different volume.
        try:
            os.replace(source, target)
        except OSError:
            shutil.copyfile(source, target)
            source.unlink(missing_ok=True)
        return target

    def touch(self, sha256: str) -> None:
        """Mark an asset as used now, for the eviction order.

        Access time would be the natural signal and is unusable: most
        filesystems are mounted `relatime` or `noatime`, so reading a file
        frequently does not reliably update it. Modification time is under our
        control, and we are the only writer.
        """
        path = self.path_for(sha256)
        if path.exists():
            moment = time.time()
            os.utime(path, (moment, moment))

    def size_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self.root.rglob("*") if path.is_file()
        )

    def sweep(self, *, budget_bytes: int | None = None) -> int:
        """Drop least-recently-used assets until the cache fits. Returns bytes freed.

        Never raises on a file that vanishes underneath it: another executor
        process, or a person tidying up, is allowed to delete from this
        directory, and a cache sweep is not a place to be strict about it.
        """
        ceiling = budget_bytes if budget_bytes is not None else self.budget_bytes
        files = [path for path in self.root.rglob("*") if path.is_file()]
        total = 0
        stamped: list[tuple[float, int, Path]] = []
        for path in files:
            try:
                info = path.stat()
            except OSError:
                continue
            total += info.st_size
            stamped.append((info.st_mtime, info.st_size, path))
        if total <= ceiling:
            return 0

        freed = 0
        for _, size, path in sorted(stamped, key=lambda item: item[0]):
            if total - freed <= ceiling:
                break
            try:
                path.unlink()
            except OSError:
                continue
            freed += size
        return freed


__all__ = ["CHUNK", "DEFAULT_BUDGET_BYTES", "AssetCache", "digest_of"]
