# The stand-in pool worker image: the pinned Python base, the hash-pinned third-party packages of wft-workflow and
# wft-aggregate, the workspace wheels, uv, and the hash-checked wasmtime and wasm-tools release binaries with their
# modification time fixed, since wasmtime names its module cache after it. One image serves the three pools here.
ARG BASE
FROM ${BASE}
COPY requirements.txt /tmp/build/
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/build/requirements.txt
COPY wheels /tmp/build/wheels
RUN pip install --no-cache-dir --no-deps --no-index /tmp/build/wheels/*.whl && rm -rf /tmp/build
ARG UV_VERSION
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
COPY bin/ /usr/local/bin/
ENV PYTHONUNBUFFERED=1
