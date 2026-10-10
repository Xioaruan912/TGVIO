from __future__ import annotations

from dataclasses import dataclass, field

from tgvio_player.domain.catalog import (
    ArchiveDiscovery,
    ArchivePackageCandidate,
)


@dataclass(slots=True)
class FakeArchiveCatalogSource:
    packages: list[ArchivePackageCandidate] = field(default_factory=list)
    complete_scan: bool = True

    async def discover(self, *, recent_dates: int | None = None) -> ArchiveDiscovery:
        return ArchiveDiscovery(
            packages=tuple(self.packages),
            complete_scan=self.complete_scan and recent_dates is None,
        )
