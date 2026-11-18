# The admission service. The context is the one image.sh prepares: requirements.txt, the hash-pinned third-party
# packages other than python-qpid-proton, proton.txt, the hash-pinned sdist of python-qpid-proton, dists/, the files of
# both, and wheels/, the workspace packages built from the tree.
ARG BASE

# python-qpid-proton publishes no Linux wheel, so this stage builds one from the sdist that uv.lock pins, checked by its
# hash, and only the wheel reaches the service image, which has no compiler. The build tools come from the network,
# then the build runs with none, without build isolation, over the cffi of the hash-pinned packages.
FROM ${BASE} AS proton
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir setuptools==80.9.0
COPY requirements.txt proton.txt /tmp/build/
RUN --network=none --mount=type=bind,source=dists,target=/tmp/dists \
    pip install --no-cache-dir --no-deps --require-hashes --no-index --find-links /tmp/dists \
    -r /tmp/build/requirements.txt \
    && pip wheel --no-cache-dir --no-deps --require-hashes --no-index --find-links /tmp/dists \
    --no-build-isolation --no-binary python-qpid-proton -w /wheels -r /tmp/build/proton.txt

FROM ${BASE}
COPY requirements.txt /tmp/build/
# The hash-pinned packages install from dists/, mounted rather than copied so its files add no layer, with no network.
RUN --network=none --mount=type=bind,source=dists,target=/tmp/dists \
    pip install --no-cache-dir --no-deps --require-hashes --no-index --find-links /tmp/dists \
    -r /tmp/build/requirements.txt
COPY --from=proton /wheels /tmp/build/proton
COPY wheels /tmp/build/wheels
RUN pip install --no-cache-dir --no-deps --no-index /tmp/build/proton/*.whl /tmp/build/wheels/*.whl && pip check \
    && rm -rf /tmp/build
ENV PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python", "-m", "wdf_admission.service"]
