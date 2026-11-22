# The throwaway wiring: the OTLP and Pyroscope client libraries of wft-obs on the pinned Python, with a sitecustomize
# that starts them in any Python process whose environment names the collector or Pyroscope. A pod copies /payload
# onto a volume with an init container and puts it on PYTHONPATH, so no service image is rebuilt.
ARG BASE
FROM ${BASE} AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ make libffi-dev && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --target /payload opentelemetry-sdk==1.45.2 opentelemetry-exporter-otlp-proto-http==1.45.2 \
    pyroscope-io==1.4.0
COPY sitecustomize.py /payload/

FROM ${BASE}
COPY --from=build /payload /payload
CMD ["cp", "-r", "/payload/.", "/wiring/"]
