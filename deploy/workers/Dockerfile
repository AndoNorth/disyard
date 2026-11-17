# The pool worker images. The context is the one image.sh prepares for a pool: requirements.txt, the hash-pinned
# third-party packages of wdf-worker with the pool's extra, wheels/, the workspace packages built from the tree, and
# bin/, the pool's release binaries with their modification time fixed. Each pool is a target, because an exec-form
# ENTRYPOINT does not expand a build argument.
ARG BASE
FROM ${BASE} AS worker
COPY requirements.txt /tmp/build/
# The hash-pinned packages install from dists/, mounted rather than copied so its files add no layer, with no network.
RUN --network=none --mount=type=bind,source=dists,target=/tmp/dists \
    pip install --no-cache-dir --no-deps --require-hashes --no-index --find-links /tmp/dists \
    -r /tmp/build/requirements.txt
COPY wheels /tmp/build/wheels
RUN pip install --no-cache-dir --no-deps --no-index /tmp/build/wheels/*.whl && pip check && rm -rf /tmp/build
COPY bin/ /usr/local/bin/
# The scratch mount point, owned by the worker so a fresh volume mounted there is writable.
RUN install -d -o 65532 -g 65532 /var/wdf
ENV PYTHONUNBUFFERED=1
USER 65532:65532

FROM worker AS compile
ENTRYPOINT ["python", "-m", "wdf_worker", "--pool", "compile"]

FROM worker AS simulate
ENTRYPOINT ["python", "-m", "wdf_worker", "--pool", "simulate"]

FROM worker AS aggregate
ENTRYPOINT ["python", "-m", "wdf_worker", "--pool", "aggregate"]
