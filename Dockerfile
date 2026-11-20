# Imagen única para la app y para correr los tests (docker compose run app pytest -q).
FROM python:3.12-slim

WORKDIR /srv

# psycopg2-binary trae su propio libpq; no se necesitan paquetes de sistema extra.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY tests/ tests/
COPY scripts/ scripts/

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /srv/run \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8011

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/health', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8011"]
