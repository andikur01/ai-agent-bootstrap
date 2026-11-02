"""Keep the disk from filling up, so a 24/7 bot does not die of its own output.

The user's worry was concrete: "takut penuh". They had already watched a free
host eat 24% of a quota in a few chats and did not want the same thing to happen
to a server that is supposed to stay up. Generated images, videos and audio are
the bulk of it, and nothing was deleting them - every render accumulated forever
until the disk filled and the bot stopped.

What must never be deleted:

* saved logins (data/profiles) - deleting them logs the user out of every site
* the memory database - deleting it loses the whole point of the bot
* the newest file of each kind - the user may still be referring to it

What is safe to delete: everything else, oldest first, once a size or age limit
is crossed. Deletion happens oldest-first and is reported, because silently
eating the user's files would be worse than filling the disk.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

# Never touched, whatever the size. Losing a login or the memory database costs
# the user far more than a full disk.
PROTECTED_NAMES = {"profiles", "memory", "chroma", "n8n"}
PROTECTED_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".env"}


@dataclass
class RetentionPolicy:
    """How much to keep. Defaults suit a small VPS or a free host."""

    max_total_mb: int = 2048  # 2 GB of outputs
    max_age_days: int = 14
    keep_latest_per_kind: int = 3  # newest image/video/audio always survive
    enabled: bool = True


@dataclass
class SweepReport:
    deleted: list[str] = field(default_factory=list)
    freed_mb: float = 0.0
    kept: int = 0

    @property
    def freed(self) -> str:
        if self.freed_mb >= 1024:
            return f"{self.freed_mb / 1024:.1f} GB"
        return f"{self.freed_mb:.1f} MB"

    def summary(self) -> str:
        if not self.deleted:
            return f"Disk rapi. {self.kept} file dipertahankan, tidak ada yang dihapus."
        return (
            f"Aku hapus {len(self.deleted)} file lama, bebaskan {self.freed}. "
            f"{self.kept} file terbaru dipertahankan."
        )


def _is_protected(path: Path, root: Path) -> bool:
    """True when deleting this file would cost the user something irreplaceable."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    if any(part in PROTECTED_NAMES for part in relative.parts):
        return True
    return path.suffix in PROTECTED_SUFFIXES


def _size_mb(path: Path) -> float:
    if path.is_file():
        try:
            return path.stat().st_size / (1024 * 1024)
        except OSError:
            return 0.0
    total = 0.0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total / (1024 * 1024)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class Retention:
    """Deletes old generated files when the outputs directory grows too large."""

    def __init__(self, outputs_dir: Path, data_dir: Path, policy: RetentionPolicy | None = None):
        self.outputs = Path(outputs_dir)
        self.data = Path(data_dir)
        self.policy = policy or RetentionPolicy()

    def _candidates(self) -> list[Path]:
        """Files that may be deleted, oldest first."""
        files: list[Path] = []
        for root in (self.outputs, self.data / "uploads"):
            if not root.exists():
                continue
            for item in root.rglob("*"):
                if item.is_file() and not _is_protected(item, root):
                    files.append(item)
        files.sort(key=_mtime)
        return files

    def _newest_per_kind(self, files: list[Path]) -> set[Path]:
        """The newest few files of each kind, kept regardless of size or age.

        The user is most likely to still be referring to the thing they just
        made, so age alone must not delete it.
        """
        by_kind: dict[str, list[Path]] = {}
        for path in files:
            by_kind.setdefault(path.parent.name, []).append(path)
        keep: set[Path] = set()
        for group in by_kind.values():
            group.sort(key=_mtime, reverse=True)
            keep.update(group[: self.policy.keep_latest_per_kind])
        return keep

    def sweep(self, *, force: bool = False) -> SweepReport:
        """Delete until the size and age limits are met. Returns what happened."""
        report = SweepReport()
        if not self.policy.enabled and not force:
            return report

        files = self._candidates()
        if not files:
            return report

        keep = self._newest_per_kind(files)
        cutoff = time.time() - self.policy.max_age_days * 86400
        total_mb = _size_mb(self.outputs) if self.outputs.exists() else 0.0

        for path in files:
            if path in keep:
                continue
            over_size = total_mb > self.policy.max_total_mb
            if not (over_size or _mtime(path) < cutoff):
                continue
            size_mb = _size_mb(path)
            try:
                path.unlink()
            except OSError as exc:  # noqa: BLE001 - a locked file is not fatal
                log.debug("retention could not delete %s: %s", path, exc)
                continue
            report.deleted.append(str(path))
            report.freed_mb += size_mb
            total_mb -= size_mb

        report.kept = len(files) - len(report.deleted)
        if report.deleted:
            log.info(
                "retention: deleted %s files, freed %.1f MB",
                len(report.deleted),
                report.freed_mb,
            )
        return report

    def status(self) -> str:
        """One line for the user, so "penuh" is never a surprise."""
        size = _size_mb(self.outputs) if self.outputs.exists() else 0.0
        limit = self.policy.max_total_mb
        pct = (size / limit * 100) if limit else 0
        return (
            f"Penyimpanan: {size:.0f} MB dari {limit} MB ({pct:.0f}%). "
            "Aku bersihkan otomatis kalau lewat batas."
        )


def build_retention(settings) -> Retention:
    policy = RetentionPolicy(
        max_total_mb=int(getattr(settings, "outputs_max_mb", 2048)),
        max_age_days=int(getattr(settings, "outputs_max_age_days", 14)),
    )
    return Retention(settings.outputs_path, settings.data_path, policy)
