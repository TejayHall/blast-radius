# Blast Radius selector. Same base image digest the commits were labeled in (its Python decides
# which sqlglot tests run and how), so our local verification runs reproduce the labels.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit (the only non-registry download, as the rules allow)
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir -p /sqlglot \
 && tar -xzf /tmp/sqlglot.tar.gz -C /sqlglot --strip-components=1 \
 && rm /tmp/sqlglot.tar.gz

# The exact environment the labels were made in (PyPI only)
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.lock \
 && (python -m compileall -q /sqlglot/sqlglot /sqlglot/tests || true)

# Deterministic set ordering, so import-time state fingerprints are reproducible between build and run
ENV SKIP_INTEGRATION=0 PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1

COPY selector /app/selector
COPY tools /app/tools
COPY data /app/data
RUN cd /app \
 && python -c "from selector.index import build_cache; build_cache()" \
 && python -c "from selector.importtime import build_keys_cache; build_keys_cache()" \
 && python -c "from selector.cases import build_cache; build_cache()" \
 && python tools/build_state.py /sqlglot \
 && python -m compileall -q /app/selector \
 && chmod -R a+rX /app /sqlglot

WORKDIR /app
CMD ["python", "-m", "selector.main"]
