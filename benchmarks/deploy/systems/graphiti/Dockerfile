# Graphiti's REST server, the zepai/graphiti image at 0.30.2, with the two fixes in patch.py (the client
# closed before the background queue used it, and a queue worker that stopped at the first failed episode).
# Nothing else changes: the same routes, the same recipes, the same graphiti-core.
#   docker build -t niadra-bench/graphiti:0.30.2 deploy/systems/graphiti
FROM zepai/graphiti:0.30.2
USER root
COPY patch.py /tmp/patch.py
RUN /app/.venv/bin/python /tmp/patch.py && rm /tmp/patch.py && chown -R app:app /app/graph_service
USER app
