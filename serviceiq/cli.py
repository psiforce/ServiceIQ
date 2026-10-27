"""ServiceIQ command line. Run `python -m serviceiq --help` to list commands."""
import typer

from serviceiq.config import ConfigError, get_settings

app = typer.Typer(help="ServiceIQ RAG MVP command line.", no_args_is_help=True, add_completion=False)


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
