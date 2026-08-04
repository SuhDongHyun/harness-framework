from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitSnapshot:
    branch: str
    head: str
    porcelain: str
    dirty_paths: tuple[str, ...]
    dirty_hashes: dict[str, str]
    fingerprint: str
    index_fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "head": self.head,
            "porcelain": self.porcelain,
            "dirty_paths": list(self.dirty_paths),
            "dirty_hashes": self.dirty_hashes,
            "fingerprint": self.fingerprint,
            "index_fingerprint": self.index_fingerprint,
        }


class GitGuard:
    """Read-only Git boundary used to protect pre-existing user changes."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def snapshot(self) -> GitSnapshot:
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        head = self._git("rev-parse", "HEAD").strip()
        porcelain = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        dirty_paths = tuple(sorted(_parse_porcelain_paths(porcelain)))
        dirty_hashes = {path: self._hash_path(path) for path in dirty_paths}
        index_fingerprint = hashlib.sha256(
            self._git("ls-files", "--stage", "-z").encode(
                "utf-8", errors="surrogateescape"
            )
        ).hexdigest()
        canonical = json.dumps(
            {
                "branch": branch,
                "head": head,
                "porcelain": porcelain,
                "dirty_hashes": dirty_hashes,
                "index_fingerprint": index_fingerprint,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return GitSnapshot(
            branch=branch,
            head=head,
            porcelain=porcelain,
            dirty_paths=dirty_paths,
            dirty_hashes=dirty_hashes,
            fingerprint=hashlib.sha256(canonical).hexdigest(),
            index_fingerprint=index_fingerprint,
        )

    @staticmethod
    def changed_paths(before: GitSnapshot, after: GitSnapshot) -> set[str]:
        before_paths = set(before.dirty_paths)
        after_paths = set(after.dirty_paths)
        changed = after_paths - before_paths
        for path in before_paths | after_paths:
            if before.dirty_hashes.get(path) != after.dirty_hashes.get(path):
                changed.add(path)
        if before.index_fingerprint != after.index_fingerprint:
            changed.update(before_paths | after_paths)
        return changed

    def _hash_path(self, relative_path: str) -> str:
        path = self.root / relative_path
        if path.is_symlink():
            return (
                "symlink:"
                + hashlib.sha256(path.readlink().as_posix().encode("utf-8")).hexdigest()
            )
        if not path.is_file():
            return "missing"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "git command failed")
        return result.stdout


def _parse_porcelain_paths(porcelain: str) -> set[str]:
    records = porcelain.split("\0")
    paths: set[str] = set()
    skip_next = False
    for record in records:
        if not record:
            continue
        if skip_next:
            paths.add(record)
            skip_next = False
            continue
        if len(record) < 4:
            continue
        status = record[:2]
        paths.add(record[3:])
        if "R" in status or "C" in status:
            skip_next = True
    return paths


def paths_outside_allowed(
    changed_paths: Iterable[str], allowed_patterns: Iterable[str]
) -> set[str]:
    patterns = tuple(allowed_patterns)
    return {
        path
        for path in changed_paths
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    }
