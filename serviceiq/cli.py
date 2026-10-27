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
