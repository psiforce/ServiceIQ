# ServiceIQ: one image for every role. The first argument picks the role (see deploy/entrypoint.sh):
#   api | chat | admin | init-db | ingest | evaluate ...
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
# requirements.lock pins the exact versions tested locally.
COPY requirements.txt requirements.lock ./
RUN pip install -r requirements.txt -c requirements.lock

COPY serviceiq ./serviceiq
COPY apps ./apps
COPY data ./data
COPY .streamlit ./.streamlit
COPY deploy/entrypoint.sh ./deploy/entrypoint.sh

# Run as an unprivileged user; var/ holds local-only runtime files (uploads, outbox, dev keys)
RUN useradd --create-home --uid 10001 serviceiq \
    && mkdir -p /app/var \
    && chown -R serviceiq /app/var \
    && chmod +x deploy/entrypoint.sh
USER serviceiq

EXPOSE 8080
ENTRYPOINT ["./deploy/entrypoint.sh"]
CMD ["api"]
