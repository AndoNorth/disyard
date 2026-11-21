# A stand-in for the state inputs exporter: the Temporal runtime's Prometheus endpoint and psycopg on the pinned Python,
# the two libraries the real exporter would load, running one SQL pass every 60 s.
ARG BASE
FROM ${BASE}
RUN pip install --no-cache-dir temporalio==1.35.0 "psycopg[binary]==3.3.6"
COPY exporter.py /app/exporter.py
RUN useradd -u 65532 -m exporter
USER 65532
ENTRYPOINT ["python", "/app/exporter.py"]
