"""Tenant manifests: one row of ingestion metadata per document."""
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManifestEntry:
    document_id: str
    tenant_id: str
    path: Path
    title: str
    source: str
    document_type: str
    authority_rank: int
    approval_status: str
    version: int
    effective_date: str
    expiry_date: str  # empty string means no expiry
    access_scope: str

    @property
    def format(self) -> str:
        return self.path.suffix.lstrip(".").lower()

    def metadata(self) -> dict:
        """Metadata attached to every Document and, later, every chunk and vector."""
        return {
            "tenant_id": self.tenant_id,
            "document_id": self.document_id,
            "title": self.title,
            "source": self.source,
            "document_type": self.document_type,
            "authority_rank": self.authority_rank,
            "approval_status": self.approval_status,
            "version": self.version,
            "effective_date": self.effective_date,
            "expiry_date": self.expiry_date,
            "access_scope": self.access_scope,
            "format": self.format,
        }


def tenant_ids(data_dir: Path) -> list[str]:
    return sorted(p.parent.name for p in (data_dir / "tenants").glob("*/manifest.csv"))


def read_manifest(data_dir: Path, tenant_id: str) -> list[ManifestEntry]:
    tenant_dir = data_dir / "tenants" / tenant_id
    with (tenant_dir / "manifest.csv").open(encoding="utf-8", newline="") as f:
        return [
            ManifestEntry(
                document_id=row["document_id"],
                tenant_id=row["tenant_id"],
                path=tenant_dir / row["file"],
                title=row["title"],
                source=row["source"],
                document_type=row["document_type"],
                authority_rank=int(row["authority_rank"]),
                approval_status=row["approval_status"],
                version=int(row["version"]),
                effective_date=row["effective_date"],
                expiry_date=row["expiry_date"],
                access_scope=row["access_scope"],
            )
            for row in csv.DictReader(f)
        ]
