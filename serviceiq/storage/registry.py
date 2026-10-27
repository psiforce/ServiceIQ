"""Document registry (BP-2 knowledge lifecycle): documents with immutable versions.

Lifecycle: upload -> pending -> approved (previous approved version becomes superseded) -> searchable;
expiry dates and retirement are managed here too. The manifests in data/tenants/*/manifest.csv seed the
registry once; after that the admin console is the source of truth.
"""
import re
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from serviceiq.config import PROJECT_ROOT
from serviceiq.ingestion.manifest import ManifestEntry, read_manifest, tenant_ids
from serviceiq.storage.models import DocumentRecord, DocumentVersionRecord

# Freshness SLA (requirements section 4): pricing, policy, warranty (and promotions, safety) within 15 minutes
FAST_TYPES = {"price_book", "policy", "warranty_policy", "promotion", "safety"}
ALLOWED_FORMATS = {"csv", "pdf", "docx", "html"}


def sla_seconds(document_type: str) -> int:
    return 15 * 60 if document_type in FAST_TYPES else 24 * 3600


def _relative(path: Path) -> str:
    """Project-relative when inside the project (portable between machines), otherwise absolute."""
    resolved = path.resolve()
    return resolved.relative_to(PROJECT_ROOT).as_posix() if resolved.is_relative_to(PROJECT_ROOT) else resolved.as_posix()


def seed_from_manifests(session: Session, data_dir: Path) -> int:
    """Copy manifest rows into the registry (only documents not registered yet)."""
    added = 0
    for tenant_id in tenant_ids(data_dir):
        for e in read_manifest(data_dir, tenant_id):
            if session.get(DocumentRecord, e.document_id) is not None:
                continue
            session.add(DocumentRecord(document_id=e.document_id, tenant_id=e.tenant_id, title=e.title, source=e.source,
                                       document_type=e.document_type, authority_rank=e.authority_rank,
                                       access_scope=e.access_scope))
            session.add(DocumentVersionRecord(document_id=e.document_id, version=e.version, file_path=_relative(e.path),
                                              approval_status=e.approval_status, effective_date=e.effective_date,
                                              expiry_date=e.expiry_date, uploaded_by="seed",
                                              approved_by="seed" if e.approval_status == "approved" else None))
            added += 1
    session.commit()
    return added


def has_registry(session: Session, tenant_id: str) -> bool:
    return session.scalar(select(DocumentRecord.document_id).where(DocumentRecord.tenant_id == tenant_id)) is not None


def current_version(doc: DocumentRecord) -> DocumentVersionRecord | None:
    approved = [v for v in doc.versions if v.approval_status == "approved"]
    return approved[-1] if approved and not doc.retired else None


def entry_for(doc: DocumentRecord, version: DocumentVersionRecord) -> ManifestEntry:
    return ManifestEntry(document_id=doc.document_id, tenant_id=doc.tenant_id, path=PROJECT_ROOT / version.file_path,
                         title=doc.title, source=doc.source, document_type=doc.document_type,
                         authority_rank=doc.authority_rank, approval_status="approved", version=version.version,
                         effective_date=version.effective_date, expiry_date=version.expiry_date,
                         access_scope=doc.access_scope)


def current_entries(session: Session, tenant_id: str) -> list[ManifestEntry]:
    """The approved, non-retired version of every document: exactly what should be searchable."""
    docs = session.scalars(select(DocumentRecord).where(DocumentRecord.tenant_id == tenant_id)
                           .order_by(DocumentRecord.document_id))
    return [entry_for(d, v) for d in docs if (v := current_version(d)) is not None]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def add_document(session: Session, tenant_id: str, title: str, document_type: str, authority_rank: int,
                 source: str = "admin-upload", access_scope: str = "public") -> DocumentRecord:
    prefix = {"cedar-ridge": "crc"}.get(tenant_id, tenant_id.split("-")[0])
    document_id, n = f"{prefix}-{slug(title)}", 2
    while session.get(DocumentRecord, document_id) is not None:
        document_id, n = f"{prefix}-{slug(title)}-{n}", n + 1
    doc = DocumentRecord(document_id=document_id, tenant_id=tenant_id, title=title, source=source,
                         document_type=document_type, authority_rank=authority_rank, access_scope=access_scope)
    session.add(doc)
    session.flush()
    return doc


def add_version(session: Session, doc: DocumentRecord, filename: str, content: bytes, effective_date: date,
                expiry_date: date | None, uploaded_by: str, uploads_dir: Path) -> DocumentVersionRecord:
    """Store an uploaded file as a new pending version (not searchable until approved)."""
    fmt = Path(filename).suffix.lstrip(".").lower()
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(f"unsupported file type '.{fmt}'; use one of {sorted(ALLOWED_FORMATS)}")
    if expiry_date and expiry_date < effective_date:
        raise ValueError("expiry date is before the effective date")
    number = max((v.version for v in doc.versions), default=0) + 1
    path = uploads_dir / doc.tenant_id / f"{doc.document_id}-v{number}.{fmt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    version = DocumentVersionRecord(document_id=doc.document_id, version=number, file_path=_relative(path),
                                    approval_status="pending", effective_date=effective_date.isoformat(),
                                    expiry_date=expiry_date.isoformat() if expiry_date else "", uploaded_by=uploaded_by)
    doc.versions.append(version)
    session.flush()
    return version


def approve(session: Session, version: DocumentVersionRecord, approver: str) -> None:
    doc = session.get(DocumentRecord, version.document_id)
    for other in doc.versions:
        if other is not version and other.approval_status == "approved":
            other.approval_status = "superseded"
    version.approval_status, version.approved_by = "approved", approver
    version.approved_at = datetime.now(timezone.utc)
    doc.retired = False


def reject(version: DocumentVersionRecord, approver: str) -> None:
    version.approval_status, version.approved_by = "rejected", approver


def set_expiry(doc: DocumentRecord, expiry: date | None) -> DocumentVersionRecord:
    current = current_version(doc)
    if current is None:
        raise ValueError("document has no approved version")
    current.expiry_date = expiry.isoformat() if expiry else ""
    return current


def retire(doc: DocumentRecord) -> None:
    doc.retired = True
