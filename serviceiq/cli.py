"""ServiceIQ command line. Run `python -m serviceiq --help` to list commands."""
import sys

import typer
from rich.console import Console
from rich.table import Table

from serviceiq.config import PROJECT_ROOT, ConfigError, get_settings

# Windows consoles default to cp1252; documents contain characters such as "≤" and "°"
sys.stdout.reconfigure(encoding="utf-8")

app = typer.Typer(help="ServiceIQ RAG MVP command line.", no_args_is_help=True, add_completion=False)
console = Console()
DATA_DIR = PROJECT_ROOT / "data"


@app.callback()
def main():
    """ServiceIQ RAG MVP command line."""


def _check_openai(settings) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key.get_secret_value())
    model_ids = {m.id for m in client.models.list()}
    if settings.openai_embedding_model not in model_ids:
        raise RuntimeError(f"embedding model {settings.openai_embedding_model} not available to this key")
    return f"{len(model_ids)} models visible, {settings.openai_embedding_model} available"


def _check_pinecone(settings) -> str:
    from pinecone import Pinecone

    names = Pinecone(api_key=settings.pinecone_api_key.get_secret_value()).list_indexes().names()
    status = "exists" if settings.pinecone_index_name in names else "not created yet (created in M4)"
    return f"{len(names)} index(es); '{settings.pinecone_index_name}' {status}"


def _check_langsmith(settings) -> str:
    from langsmith import Client

    if settings.langsmith_api_key is None:
        return "skipped (no LANGSMITH_API_KEY; optional until Sprint 2)"
    next(iter(Client(api_key=settings.langsmith_api_key.get_secret_value()).list_projects(limit=1)), None)
    return "authenticated"


@app.command()
def check():
    """Verify configuration and connectivity to OpenAI, Pinecone and LangSmith (free calls only)."""
    try:
        settings = get_settings()
    except ConfigError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1)

    failed = False
    for name, probe in [("OpenAI", _check_openai), ("Pinecone", _check_pinecone), ("LangSmith", _check_langsmith)]:
        try:
            typer.secho(f"  OK    {name:<10} {probe(settings)}", fg="green")
        except Exception as exc:  # report every service, don't stop at the first failure
            failed = True
            typer.secho(f"  FAIL  {name:<10} {type(exc).__name__}: {exc}", fg="red")
    raise typer.Exit(1 if failed else 0)


@app.command()
def load(
    tenant: str = typer.Option("all", help="Tenant ID, or 'all'."),
    show: str = typer.Option(None, help="Print the loaded text of one document ID."),
):
    """M1: load documents from the tenant manifests and show what was extracted."""
    from serviceiq.ingestion.loaders import load_all

    docs = load_all(DATA_DIR, tenant)
    if show:
        match = next((d for d in docs if d.entry.document_id == show), None)
        if match is None:
            raise typer.BadParameter(f"unknown document ID: {show}")
        for part in match.parts:
            console.rule(f"{show} - part {part.metadata['part']}")
            console.print(part.page_content, markup=False, highlight=False)
        console.print(match.parts[0].metadata)
        return

    table = Table(title=f"Loaded documents ({tenant})")
    for col in ["document_id", "fmt", "parts", "chars", "type", "status", "effective", "expiry", "scope"]:
        table.add_column(col, no_wrap=col in {"document_id", "effective", "expiry"}, overflow="fold")
    for d in docs:
        e = d.entry
        table.add_row(e.document_id, e.format, str(len(d.parts)), str(d.char_count), e.document_type,
                      e.approval_status, e.effective_date, e.expiry_date or "-", e.access_scope)
    console.print(table)
    console.print(f"{len(docs)} documents, {sum(len(d.parts) for d in docs)} parts, "
                  f"{sum(d.char_count for d in docs):,} characters")


@app.command()
def clean(
    tenant: str = typer.Option("all", help="Tenant ID, or 'all'."),
    doc: str = typer.Option(None, help="Show one document before and after cleaning."),
):
    """M2: clean documents (remove boilerplate, HTML to Markdown, hash) and report what changed."""
    from serviceiq.ingestion.cleaning import clean_documents
    from serviceiq.ingestion.loaders import load_all

    raw = load_all(DATA_DIR, tenant)
    cleaned, stats = clean_documents(raw)
    if doc:
        before = next((d for d in raw if d.entry.document_id == doc), None)
        after = next((d for d in cleaned if d.entry.document_id == doc), None)
        if before is None:
            raise typer.BadParameter(f"unknown document ID: {doc}")
        console.rule(f"BEFORE: {doc} ({before.char_count} chars)")
        console.print(before.parts[0].page_content, markup=False, highlight=False)
        console.rule(f"AFTER: {doc} ({after.char_count} chars)")
        console.print(after.parts[0].page_content if after.parts else "(no content)", markup=False, highlight=False)
        if after.parts:
            console.print(f"content_hash: {after.parts[0].metadata['content_hash']}")
        return

    table = Table(title=f"Cleaning report ({tenant})")
    for col in ["document_id", "fmt", "chars before", "chars after", "removed", "repeated lines", "duplicate parts"]:
        table.add_column(col, no_wrap=col == "document_id")
    for d in cleaned:
        s = stats[d.entry.document_id]
        removed = 1 - s.chars_after / s.chars_before if s.chars_before else 0
        table.add_row(d.entry.document_id, d.entry.format, str(s.chars_before), str(s.chars_after), f"{removed:.0%}",
                      str(s.repeated_lines_removed), ",".join(map(str, s.duplicate_parts)) or "-")
    console.print(table)
    before_total = sum(s.chars_before for s in stats.values())
    after_total = sum(s.chars_after for s in stats.values())
    console.print(f"{before_total:,} -> {after_total:,} characters ({1 - after_total / before_total:.0%} removed)")


@app.command()
def chunk(
    tenant: str = typer.Option("all", help="Tenant ID, or 'all'."),
    doc: str = typer.Option(None, help="Print every chunk of one document ID."),
):
    """M3: split cleaned documents into chunks; writes reports/chunks.csv."""
    import csv
    import statistics
    from collections import Counter

    from serviceiq.ingestion.pipeline import prepare_chunks

    settings = get_settings()
    chunks = prepare_chunks(settings, tenant)
    if doc:
        selected = [c for c in chunks if c.metadata["document_id"] == doc]
        if not selected:
            raise typer.BadParameter(f"unknown document ID: {doc}")
        for c in selected:
            m = c.metadata
            console.rule(f"{m['chunk_id']}  |  {m['token_count']} tokens  |  section: {m['section'] or '-'}")
            console.print(c.page_content, markup=False, highlight=False)
        return

    report = PROJECT_ROOT / "reports" / "chunks.csv"
    report.parent.mkdir(exist_ok=True)
    with report.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["chunk_id", "tenant_id", "document_id", "document_type", "section", "token_count", "text"])
        for c in chunks:
            m = c.metadata
            writer.writerow([m["chunk_id"], m["tenant_id"], m["document_id"], m["document_type"], m["section"],
                             m["token_count"], c.page_content])

    tokens = [c.metadata["token_count"] for c in chunks]
    table = Table(title=f"Chunks by document type ({tenant}), target {settings.chunk_target_tokens} tokens, "
                        f"overlap {settings.chunk_overlap_tokens}")
    for col in ["document_type", "documents", "chunks", "avg tokens", "max tokens"]:
        table.add_column(col)
    by_type: dict[str, list] = {}
    for c in chunks:
        by_type.setdefault(c.metadata["document_type"], []).append(c)
    for doc_type, items in sorted(by_type.items()):
        t = [c.metadata["token_count"] for c in items]
        table.add_row(doc_type, str(len({c.metadata["document_id"] for c in items})), str(len(items)),
                      str(round(statistics.mean(t))), str(max(t)))
    console.print(table)
    buckets = Counter(min(n // 100 * 100, 500) for n in tokens)
    console.print("Token distribution: " + "  ".join(f"{b}-{b + 99}: {buckets[b]}" for b in sorted(buckets)))
    console.print(f"{len(chunks)} chunks | min {min(tokens)} | median {statistics.median(tokens):.0f} | "
                  f"max {max(tokens)} tokens | written to {report.relative_to(PROJECT_ROOT)}")


@app.command()
def ingest(tenant: str = typer.Option("all", help="Tenant ID, or 'all'.")):
    """M4: run load -> clean -> chunk -> embed -> index and sync each tenant's Pinecone namespace."""
    from serviceiq.ingestion.pipeline import ingest as run_ingest

    reports = run_ingest(get_settings(), tenant)
    table = Table(title="Ingestion")
    for col in ["tenant (namespace)", "approved chunks", "embedded", "metadata updated", "unchanged", "deleted",
                "tokens", "cost", "seconds"]:
        table.add_column(col)
    for r in reports:
        table.add_row(r.tenant_id, str(r.chunks), str(r.embedded), str(r.metadata_updated), str(r.unchanged), str(r.deleted),
                      str(r.embedding_tokens), f"${r.embedding_cost:.5f}", f"{r.seconds:.1f}")
    console.print(table)


@app.command("index-stats")
def index_stats():
    """M4: show vector counts per tenant namespace in Pinecone."""
    from serviceiq.ingestion.indexing import get_index

    settings = get_settings()
    stats = get_index(settings).describe_index_stats()
    console.print(f"Index '{settings.pinecone_index_name}': dimension {stats.dimension}, "
                  f"{stats.total_vector_count} vectors")
    for name, ns in sorted(stats.namespaces.items()):
        console.print(f"  namespace {name}: {ns.vector_count} vectors")


@app.command()
def search(
    query: str,
    tenant: str = typer.Option("cedar-ridge", help="Tenant namespace to search."),
    top_k: int = typer.Option(10, help="Number of chunks to return."),
):
    """M5: semantic search with tenant, approval, date and access filters."""
    from serviceiq.retrieval.retriever import Retriever

    results = Retriever(get_settings()).search(query, tenant, top_k=top_k)
    table = Table(title=f"'{query}' in {tenant}")
    for col in ["#", "score", "chunk_id", "type", "section", "text"]:
        table.add_column(col, no_wrap=col in {"chunk_id", "score"}, overflow="fold")
    for i, c in enumerate(results, 1):
        snippet = " ".join(c.text.split("\n\n", 1)[-1].split())[:160]
        table.add_row(str(i), f"{c.score:.3f}", c.chunk_id, c.metadata["document_type"], c.metadata["section"],
                      snippet)
    console.print(table)


@app.command("eval-retrieval")
def eval_retrieval(
    split: str = typer.Option("dev", help="dev, heldout or all."),
    tenant: str = typer.Option("cedar-ridge", help="Tenant the golden set belongs to."),
    k: int = typer.Option(5, help="Recall is measured on the top k chunks."),
):
    """M5: Recall@k, Hit@k and MRR on the golden questions; writes reports/retrieval_<split>.csv."""
    from serviceiq.evaluation.datasets import load_golden
    from serviceiq.evaluation.retrieval_eval import evaluate_retrieval, write_details
    from serviceiq.retrieval.retriever import Retriever

    settings = get_settings()
    retriever = Retriever(settings)
    report = evaluate_retrieval(retriever, load_golden(DATA_DIR, split), tenant, DATA_DIR,
                                settings.serviceiq_as_of_date, k=k)
    out = PROJECT_ROOT / "reports" / f"retrieval_{split}.csv"
    write_details(report, out)

    table = Table(title=f"Retrieval quality - {split} split ({len(report.results)} questions, "
                        f"{len(report.scored)} with labeled sources)")
    for col in ["category", "questions", f"Recall@{k}", f"Hit@{k}", "MRR"]:
        table.add_column(col)
    categories = sorted({r.question.category for r in report.scored})
    for cat in categories:
        rows = [r for r in report.scored if r.question.category == cat]
        table.add_row(cat, str(len(rows)), f"{report.mean('recall', rows):.0%}", f"{report.mean('hit', rows):.0%}",
                      f"{report.mean('reciprocal_rank', rows):.2f}")
    table.add_row("[bold]overall[/bold]", str(len(report.scored)), f"[bold]{report.mean('recall'):.0%}[/bold]",
                  f"{report.mean('hit'):.0%}", f"{report.mean('reciprocal_rank'):.2f}")
    console.print(table)
    target_ok = report.mean("recall") >= 0.90
    console.print(f"Target Recall@5 >= 90%: [{'green' if target_ok else 'yellow'}]"
                  f"{'met' if target_ok else 'not met yet'}[/]")
    console.print(f"Leaks (draft/expired/internal/other-tenant documents retrieved): "
                  f"[{'red' if report.leaks else 'green'}]{len(report.leaks)}[/]")
    cost = retriever.embedding_tokens / 1_000_000 * 0.02
    console.print(f"Details: {out.relative_to(PROJECT_ROOT)} | query embedding cost ${cost:.5f}")


@app.command()
def guard(message: str, tenant: str = typer.Option("cedar-ridge", help="Tenant whose contact details to use.")):
    """M6: show the input guard's decision for one message."""
    from serviceiq.guardrails.guard import check_message
    from serviceiq.llm import openai_client
    from serviceiq.tenants import load_profile

    settings = get_settings()
    result = check_message(message, load_profile(DATA_DIR, tenant), openai_client(settings), settings.guard_model)
    color = {"emergency": "red", "answer": "green"}.get(result.action, "yellow")
    console.print(f"Action: [bold {color}]{result.action}[/]"
                  + (f"  (emergency: {result.emergency_type}, priority {result.escalation_priority})"
                     if result.emergency_type else ""))
    console.print(f"Injection suspected: {result.injection_suspected} | PII found: {sorted(result.pii_types) or '-'}")
    console.print(f"Logged as: {result.redacted_text}", markup=False)
    for reason in result.reasons:
        console.print(f"  - {reason}", markup=False)
    if result.message:
        console.rule("Response shown to the customer")
        console.print(result.message, markup=False)
    if result.usage:
        console.print(f"Cost: ${result.usage.cost:.5f}")


def print_response(r, show_debug: bool = False) -> None:
    color = {"answered": "green", "fallback": "yellow", "emergency": "red"}.get(r.status, "yellow")
    console.print(f"Status: [bold {color}]{r.status}[/]"
                  + (f"   Escalation: {r.escalation['priority']} - {r.escalation['reason']}" if r.escalation else ""))
    console.rule("Answer")
    console.print(r.answer, markup=False, highlight=False)
    if r.citations:
        console.rule("Sources")
        for c in r.citations:
            where = f" > {c.section}" if c.section else ""
            console.print(f"[{c.n}] {c.title}{where}  ({c.document_id} v{c.version}, {c.source})",
                          markup=False, highlight=False)
            console.print(f"     \"{c.snippet[:110]}...\"", markup=False, highlight=False, style="dim")
    console.rule()
    latency = ", ".join(f"{k} {v} ms" for k, v in r.latency_ms.items())
    console.print(f"Cost ${r.cost_usd:.5f} | {latency} | trace {r.trace_id}")
    if show_debug:
        for key, value in r.debug.items():
            console.print(f"  {key}: {value}", markup=False, highlight=False)


@app.command()
def ask(
    question: str,
    tenant: str = typer.Option("cedar-ridge", help="Tenant whose knowledge base answers."),
    debug: bool = typer.Option(False, help="Show guard reasons, retrieved chunks and grounding checks."),
):
    """M7: ask the assistant a question (guard -> retrieve -> grounded answer with citations)."""
    from serviceiq.assistant import Assistant

    print_response(Assistant(get_settings()).ask(question, tenant), debug)


@app.command()
def chat(
    tenant: str = typer.Option("cedar-ridge", help="Tenant whose knowledge base answers."),
    debug: bool = typer.Option(False, help="Show debug details after each answer."),
):
    """Multi-turn conversation in the terminal (follow-ups use earlier turns). Empty line to quit."""
    from serviceiq.assistant import Assistant

    assistant, history = Assistant(get_settings()), []
    while True:
        question = typer.prompt("\nYou", default="", show_default=False).strip()
        if not question:
            break
        response = assistant.ask(question, tenant, history=history)
        if response.debug.get("standalone_question"):
            console.print(f"(understood as: {response.debug['standalone_question']})", style="dim")
        print_response(response, debug)
        if response.status in ("answered", "fallback"):
            history.append((question, response.answer))


@app.command()
def evaluate(
    split: str = typer.Option("dev", help="dev, heldout or all. Keep heldout for acceptance runs."),
    extras: bool = typer.Option(True, help="Also run the adversarial and multi-turn sets."),
    langsmith: bool = typer.Option(True, help="Upload results as a LangSmith experiment (needs LANGSMITH_API_KEY)."),
):
    """M9: full evaluation (faithfulness, Recall@5, correctness, citations, critical errors, latency, cost)."""
    from serviceiq.assistant import Assistant
    from serviceiq.evaluation.run_eval import evaluate as run_evaluation
    from serviceiq.evaluation.run_eval import upload_to_langsmith, write_report

    settings = get_settings()
    with console.status("Evaluating...") as status:
        run = run_evaluation(Assistant(settings), split, judge_model=settings.judge_model, include_extras=extras,
                             progress=lambda msg: status.update(f"Evaluating... {msg}"))
    report = write_report(run, PROJECT_ROOT / "reports")
    console.print(report.read_text(encoding="utf-8").split("## By category")[0], markup=False, highlight=False)
    total = sum(r.response.cost_usd for r in run.golden) + sum(a["cost"] for a in run.adversarial) \
        + sum(m["cost"] for m in run.multiturn) + (run.judge_usage.cost or 0)
    console.print(f"Full report: {report.relative_to(PROJECT_ROOT)} (+ .csv) | total run cost ${total:.3f}")
    if langsmith and settings.langsmith_api_key:
        try:
            name = upload_to_langsmith(run, settings.langsmith_api_key.get_secret_value())
            console.print(f"LangSmith experiment: {name}")
        except Exception as exc:
            console.print(f"[yellow]LangSmith upload failed: {type(exc).__name__}: {exc}[/]")


@app.command("experiment-retrieval")
def experiment_retrieval(split: str = typer.Option("dev", help="Golden split to use.")):
    """M10: compare retrieval strategies (per-document caps, deeper pools, reranking) by Recall@5."""
    from serviceiq.evaluation.experiments import retrieval_experiment

    with console.status("Retrieving and reranking each question once..."):
        results = retrieval_experiment(get_settings(), split)
    baseline = results[0].recall
    table = Table(title=f"Retrieval experiment ({split} split)")
    for col in ["variant", "Recall", "vs baseline", "Hit", "rerank latency", "questions with misses"]:
        table.add_column(col, overflow="fold")
    for r in results:
        label = "Recall@5" if r.evidence_k == 5 else f"Recall@{r.evidence_k}"
        table.add_row(r.name, f"{r.recall:.1%} ({label})", f"{(r.recall - baseline) * 100:+.1f} pts", f"{r.hit:.0%}",
                      f"{r.rerank_ms:.0f} ms" if r.rerank_ms else "-", f"{len(r.misses)}: {', '.join(r.misses[:8])}"
                      + ("..." if len(r.misses) > 8 else ""))
    console.print(table)


@app.command()
def compare(
    variant: list[str] = typer.Option(..., help='"name" or "name:key=value,key=value" with Settings overrides, '
                                                 'e.g. "rerank:reranker=bge-reranker-v2-m3,max_chunks_per_doc=1".'),
    split: str = typer.Option("dev", help="Golden split to use."),
    langsmith: bool = typer.Option(True, help="Upload each variant as a LangSmith experiment."),
):
    """M10: run the end-to-end evaluation for several configurations and write a comparison report."""
    from datetime import datetime

    from serviceiq.assistant import Assistant
    from serviceiq.config import load_settings
    from serviceiq.evaluation.run_eval import TARGETS, evaluate as run_evaluation, upload_to_langsmith, write_report

    rows = []
    for spec in variant:
        name, _, overrides_text = spec.partition(":")
        overrides = dict(item.split("=", 1) for item in overrides_text.split(",") if item)
        settings = load_settings(**overrides)
        with console.status(f"[{name}] evaluating...") as status:
            run = run_evaluation(Assistant(settings), split, judge_model=settings.judge_model, include_extras=False,
                                 progress=lambda msg: status.update(f"[{name}] {msg}"))
        report = write_report(run, PROJECT_ROOT / "reports")
        if langsmith and settings.langsmith_api_key:
            try:
                upload_to_langsmith(run, settings.langsmith_api_key.get_secret_value())
            except Exception as exc:
                console.print(f"[yellow]LangSmith upload failed for {name}: {exc}[/]")
        rows.append((name, overrides, run.metrics, report))
        console.print(f"[{name}] done: correctness {run.metrics['answer_correctness']:.1%}, "
                      f"critical {run.metrics['critical_errors']}, P95 {run.metrics['p95_latency_ms']} ms")

    def fmt(key, value):
        if value is None:
            return "n/a"
        if key in ("critical_errors",):
            return str(value)
        if key.endswith("_ms"):
            return f"{value} ms"
        if key == "avg_cost_per_answer":
            return f"${value:.4f}"
        return f"{value:.1%}"

    keys = ["faithfulness", "recall_at_5", "answer_correctness", "citation_accuracy", "unsupported_handling",
            "emergency_handling", "critical_errors", "p50_latency_ms", "p95_latency_ms", "avg_cost_per_answer"]
    lines = [f"# Configuration comparison ({split} split, {datetime.now():%Y-%m-%d %H:%M})", "",
             "| Metric | Target | " + " | ".join(name for name, *_ in rows) + " |",
             "|---|---|" + "---|" * len(rows)]
    for key in keys:
        target = TARGETS.get(key)
        target_text = "-" if target is None else (f"≥ {target[0]:.0%}" if target[1] else
                                                  (str(target[0]) if key == "critical_errors" else f"≤ {target[0]} ms"))
        cells = []
        for _, _, m, _ in rows:
            met = m["targets_met"].get(key)
            cells.append(fmt(key, m[key]) + ("" if met is None else (" ✅" if met else " ❌")))
        lines.append(f"| {key} | {target_text} | " + " | ".join(cells) + " |")
    lines += ["", "Variants:"] + [f"- **{n}**: {o or 'current settings'} → `{r.name}`" for n, o, _, r in rows]
    out = PROJECT_ROOT / "reports" / f"compare_{split}_{datetime.now():%Y%m%d-%H%M}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    console.print("\n".join(lines), markup=False, highlight=False)
    console.print(f"\nComparison report: {out.relative_to(PROJECT_ROOT)}")


@app.command("init-db")
def init_db(rotate: bool = typer.Option(False, help="Issue new widget keys for existing tenants.")):
    """M11: create the database and provision tenants; prints each new widget key once."""
    import json

    from serviceiq.ingestion.manifest import tenant_ids
    from serviceiq.storage.db import make_engine, provision_tenant, session_factory
    from serviceiq.tenants import load_profile

    settings = get_settings()
    # In the cloud, keys and passwords are generated ahead of time and supplied from Secret Manager
    given_keys = json.loads(settings.chat_widget_keys) if settings.chat_widget_keys else {}
    given_passwords = json.loads(settings.admin_passwords.get_secret_value()) if settings.admin_passwords else {}
    keys_file = PROJECT_ROOT / "var" / "dev_widget_keys.json"
    keys = json.loads(keys_file.read_text(encoding="utf-8")) if keys_file.exists() else {}
    with session_factory(make_engine(settings.database_url))() as session:
        for tenant_id in tenant_ids(DATA_DIR):
            key = provision_tenant(session, tenant_id, load_profile(DATA_DIR, tenant_id).company_name, rotate,
                                   key=given_keys.get(tenant_id))
            if key:
                keys[tenant_id] = key
                console.print(f"{tenant_id}: new widget key issued")
            else:
                console.print(f"{tenant_id}: already provisioned")
    keys_file.parent.mkdir(exist_ok=True)
    keys_file.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    from sqlalchemy.engine import make_url

    console.print(f"Database: {make_url(settings.database_url).render_as_string(hide_password=True)}\n"
                  f"Dev widget keys saved to {keys_file.relative_to(PROJECT_ROOT)} "
                  "(git-ignored; in production give each key to its tenant and keep only the hash).")

    # M12: document registry and demo admin accounts
    import secrets

    from serviceiq.storage.auth import create_admin
    from serviceiq.storage.models import AdminUser
    from serviceiq.storage.registry import seed_from_manifests

    creds_file = PROJECT_ROOT / "var" / "dev_admin_credentials.json"
    creds = json.loads(creds_file.read_text(encoding="utf-8")) if creds_file.exists() else {}
    with session_factory(make_engine(settings.database_url))() as session:
        console.print(f"Registry: {seed_from_manifests(session, DATA_DIR)} document(s) added from manifests")
        for username, tenant_id, role in [("cedar-admin", "cedar-ridge", "admin"), ("cedar-csr", "cedar-ridge", "csr"),
                                          ("harbor-admin", "harbor-point", "admin")]:
            if session.get(AdminUser, username) is None or username in given_passwords:
                password = given_passwords.get(username) or secrets.token_urlsafe(12)
                create_admin(session, username, tenant_id, role, password)
                if username not in given_passwords:  # supplied passwords are never written out
                    creds[username] = {"tenant": tenant_id, "role": role, "password": password}
    creds_file.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    console.print(f"Demo admin logins saved to {creds_file.relative_to(PROJECT_ROOT)} (git-ignored).")


@app.command("create-admin")
def create_admin_command(username: str, tenant: str = typer.Option(...), role: str = typer.Option("admin")):
    """M12: create or reset an admin console account (prompts for the password)."""
    from serviceiq.storage.auth import create_admin
    from serviceiq.storage.db import make_engine, session_factory

    password = typer.prompt("Password (min 10 characters)", hide_input=True, confirmation_prompt=True)
    with session_factory(make_engine(get_settings().database_url))() as session:
        create_admin(session, username.lower(), tenant, role, password)
    console.print(f"Account '{username.lower()}' ({role}) ready for {tenant}.")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """M11: run the ServiceIQ API (docs at http://127.0.0.1:8000/docs)."""
    import uvicorn

    uvicorn.run("serviceiq.api.main:app", host=host, port=port, reload=reload)


@app.command()
def purge(days: int = typer.Option(None, help="Retention in days (default: RETENTION_DAYS, 90).")):
    """M11: delete conversations, messages, feedback and tickets older than the retention period."""
    from serviceiq.storage.db import make_engine, purge_old_conversations, session_factory

    settings = get_settings()
    with session_factory(make_engine(settings.database_url))() as session:
        removed = purge_old_conversations(session, days or settings.retention_days)
    console.print(f"Purged {removed} conversation(s) older than {days or settings.retention_days} days.")


@app.command("eval-guard")
def eval_guard():
    """M6: run all golden and adversarial messages through the guard; writes reports/guard_eval.csv."""
    import csv

    from serviceiq.evaluation.guard_eval import run_guard_eval
    from serviceiq.llm import openai_client

    settings = get_settings()
    cases, usage = run_guard_eval(DATA_DIR, openai_client(settings), settings.guard_model)
    out = PROJECT_ROOT / "reports" / "guard_eval.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "passed", "expected", "action", "emergency_type", "injection_suspected", "text",
                    "reasons"])
        for c in cases:
            w.writerow([c.case_id, c.passed, "|".join(sorted(c.expected)), c.result.action, c.result.emergency_type,
                        c.result.injection_suspected, c.text, " / ".join(c.result.reasons)])

    emergencies = [c for c in cases if c.expected == {"emergency"}]
    others = [c for c in cases if "emergency" not in c.expected]
    caught = sum(c.result.action == "emergency" for c in emergencies)
    false_alarms = [c for c in others if c.result.action == "emergency"]
    failed = [c for c in cases if not c.passed]
    console.print(f"Emergency recall:      {caught}/{len(emergencies)}")
    console.print(f"False emergencies:     {len(false_alarms)}/{len(others)}")
    console.print(f"Overall correct:       {len(cases) - len(failed)}/{len(cases)}")
    for c in failed:
        console.print(f"  [yellow]MISS[/] {c.case_id}: expected {sorted(c.expected)}, got {c.result.action}"
                      f"{' (injection flag missing)' if c.result.action in c.expected else ''} | {c.text}",
                      highlight=False)
    console.print(f"Details: {out.relative_to(PROJECT_ROOT)} | {settings.guard_model} cost ${usage.cost:.4f}")
