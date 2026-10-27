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
    for col in ["tenant (namespace)", "approved chunks", "embedded", "unchanged", "deleted", "tokens", "cost", "seconds"]:
        table.add_column(col)
    for r in reports:
        table.add_row(r.tenant_id, str(r.chunks), str(r.embedded), str(r.unchanged), str(r.deleted),
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
