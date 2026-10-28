"""M12: document registry, per-document re-indexing, admin accounts and the admin console (no network)."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from serviceiq.config import PROJECT_ROOT, get_settings, load_settings
from serviceiq.ingestion.indexing import SyncReport, wait_until_searchable
from serviceiq.ingestion.pipeline import ingest_document
from serviceiq.storage import registry
from serviceiq.storage.auth import authenticate, create_admin, hash_password, verify_password
from serviceiq.storage.db import make_engine, provision_tenant, session_factory
from serviceiq.storage.models import DocumentRecord
from tests.test_indexing import FakeIndex, fake_embed

DATA = PROJECT_ROOT / "data"


class SearchableFakeIndex(FakeIndex):
    """FakeIndex plus the calls used by metadata updates and the searchability check."""

    def list(self, namespace="", prefix=None):
        ids = [i for i in self.store.get(namespace, {}) if prefix is None or i.startswith(prefix)]
        yield SimpleNamespace(vectors=[SimpleNamespace(id=i) for i in ids])

    def update(self, id, set_metadata, namespace=""):
        self.calls.append("update")
        self.store[namespace][id]["metadata"] = set_metadata

    def fetch(self, ids, namespace=""):
        ns = self.store.get(namespace, {})
        return SimpleNamespace(vectors={i: SimpleNamespace(metadata=ns[i]["metadata"], values=ns[i]["values"])
                                        for i in ids if i in ns})

    def query(self, vector, top_k, namespace, filter):
        cid = filter["chunk_id"]["$eq"]
        return SimpleNamespace(matches=[SimpleNamespace(id=cid)] if cid in self.store.get(namespace, {}) else [])


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'admin.db').as_posix()}")
    with session_factory(engine)() as s:
        for t in ("cedar-ridge", "harbor-point"):
            provision_tenant(s, t, t)
        registry.seed_from_manifests(s, DATA)
        yield s


@pytest.fixture
def settings(tmp_path):
    return load_settings(database_url=f"sqlite:///{(tmp_path / 'admin.db').as_posix()}")


def test_seed_registers_every_manifest_document(db):
    assert db.query(DocumentRecord).count() == 65
    live = {e.document_id for e in registry.current_entries(db, "cedar-ridge")}
    assert "crc-price-book-2027-draft" not in live and "crc-price-book-2026" in live and len(live) == 59
    assert registry.seed_from_manifests(db, DATA) == 0  # idempotent


def test_upload_validation_and_approval_supersedes(db, tmp_path):
    doc = db.get(DocumentRecord, "hp-price-book-2026")
    with pytest.raises(ValueError, match="unsupported file type"):
        registry.add_version(db, doc, "prices.xlsx", b"x", date(2026, 1, 1), None, "u", tmp_path)
    with pytest.raises(ValueError, match="before the effective date"):
        registry.add_version(db, doc, "p.csv", b"x", date(2026, 5, 1), date(2026, 1, 1), "u", tmp_path)
    v2 = registry.add_version(db, doc, "p.csv", b"item_code,price\nA,1\n", date(2026, 10, 1), None, "u", tmp_path)
    assert v2.version == 2 and v2.approval_status == "pending" and registry.current_version(doc).version == 1
    registry.approve(db, v2, "boss")
    assert registry.current_version(doc) is v2 and doc.versions[0].approval_status == "superseded"


def test_new_document_ids_are_unique(db):
    a = registry.add_document(db, "cedar-ridge", "Winter Special", "promotion", 2)
    b = registry.add_document(db, "cedar-ridge", "Winter Special", "promotion", 2)
    assert (a.document_id, b.document_id) == ("crc-winter-special", "crc-winter-special-2")


def test_reindexing_one_document_leaves_others_alone(db, settings, tmp_path):
    index = SearchableFakeIndex()
    for doc_id in ("hp-price-book-2026", "hp-faq"):
        ingest_document(settings, db, doc_id, index=index, embed=fake_embed)
    faq_before = {i for i in index.store["harbor-point"] if i.startswith("hp-faq:")}

    doc = db.get(DocumentRecord, "hp-price-book-2026")
    original = (DATA / "tenants/harbor-point/raw/hp-price-book-2026.csv").read_text(encoding="utf-8")
    v2 = registry.add_version(db, doc, "p.csv", original.replace(",99,99,", ",109,109,").encode(), date(2026, 10, 26),
                              None, "u", tmp_path)
    registry.approve(db, v2, "boss")
    db.commit()
    report = ingest_document(settings, db, "hp-price-book-2026", index=index, embed=fake_embed)
    ids = set(index.store["harbor-point"])
    assert all(":2:" in i for i in ids if i.startswith("hp-price-book-2026:")) and report.deleted > 0
    assert {i for i in ids if i.startswith("hp-faq:")} == faq_before
    assert v2.ingest_seconds is not None and v2.ingested_at is not None
    live_text = " ".join(v["metadata"]["text"] for k, v in index.store["harbor-point"].items()
                         if k.startswith("hp-price-book-2026:"))
    assert "price_low_usd: 109" in live_text and "price_low_usd: 99\n" not in live_text


def test_expiry_change_updates_metadata_without_reembedding(db, settings):
    index = SearchableFakeIndex()
    ingest_document(settings, db, "hp-faq", index=index, embed=fake_embed)
    embedded_before = fake_embed.calls
    registry.set_expiry(db.get(DocumentRecord, "hp-faq"), date(2026, 11, 1))
    db.commit()
    report = ingest_document(settings, db, "hp-faq", index=index, embed=fake_embed)
    assert report.embedded == 0 and report.metadata_updated > 0 and fake_embed.calls == embedded_before
    assert all(v["metadata"]["expiry_num"] == 20261101 for v in index.store["harbor-point"].values())


def test_retire_removes_document_from_search(db, settings):
    index = SearchableFakeIndex()
    ingest_document(settings, db, "hp-faq", index=index, embed=fake_embed)
    registry.retire(db.get(DocumentRecord, "hp-faq"))
    db.commit()
    report = ingest_document(settings, db, "hp-faq", index=index, embed=fake_embed)
    assert report.deleted > 0 and not [i for i in index.store["harbor-point"] if i.startswith("hp-faq:")]


def test_wait_until_searchable_times_out_when_index_lags():
    lagging = SimpleNamespace(fetch=lambda ids, namespace: SimpleNamespace(vectors={}))
    report = SyncReport("t", upserted_ids=["doc:2:0"])
    assert wait_until_searchable(lagging, "t", report, timeout=0.3, poll=0.1) is False
    assert wait_until_searchable(lagging, "t", SyncReport("t")) is True  # nothing changed: nothing to wait for


def test_passwords_and_roles(db):
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored) and not verify_password("wrong", stored)
    with pytest.raises(ValueError, match="at least 10"):
        create_admin(db, "x", "cedar-ridge", "admin", "short")
    with pytest.raises(ValueError, match="role"):
        create_admin(db, "x", "cedar-ridge", "owner", "long enough password")
    create_admin(db, "dana", "cedar-ridge", "csr", "long enough password")
    assert authenticate(db, "DANA ", "long enough password").role == "csr"
    assert authenticate(db, "dana", "nope") is None and authenticate(db, "ghost", "whatever pass") is None


def test_admin_console_login_and_role_scoping(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    url = f"sqlite:///{(tmp_path / 'ui.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("FSP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fsp.db').as_posix()}")  # never the dev file
    monkeypatch.setenv("FSP_API_KEYS", "{}")  # no platform keys: the Schedule tab must degrade, not crash
    get_settings.cache_clear()
    with session_factory(make_engine(url))() as s:
        provision_tenant(s, "cedar-ridge", "Cedar Ridge")
        registry.seed_from_manifests(s, DATA)
        create_admin(s, "boss", "cedar-ridge", "admin", "long enough password")
        create_admin(s, "agent", "cedar-ridge", "csr", "long enough password")
    try:
        for username, expected_tabs in [("agent", ["Ticket inbox", "Service requests", "Schedule"]),
                                        ("boss", ["Ticket inbox", "Service requests", "Schedule", "Knowledge",
                                                  "Analytics"])]:
            at = AppTest.from_file(str(PROJECT_ROOT / "apps" / "admin.py"), default_timeout=60)
            at.run()
            at.text_input[0].set_value(username)
            at.text_input[1].set_value("long enough password")
            at.button[0].click().run()
            assert not at.exception
            assert [t.label for t in at.tabs] == expected_tabs
        bad = AppTest.from_file(str(PROJECT_ROOT / "apps" / "admin.py"), default_timeout=60)
        bad.run()
        bad.text_input[0].set_value("boss")
        bad.text_input[1].set_value("wrong password!")
        bad.button[0].click().run()
        assert bad.error and "Invalid" in bad.error[0].value
    finally:
        get_settings.cache_clear()
