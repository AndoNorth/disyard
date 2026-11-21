# The workflow worker service. The context is the one image.sh prepares: requirements.txt, the hash-pinned
# third-party packages, and wheels/, the workspace packages built from the tree.
ARG BASE
FROM ${BASE}
ARG WFT_BUILD_ID
COPY requirements.txt /tmp/build/
# The hash-pinned packages install from dists/, mounted rather than copied so its files add no layer, with no network.
RUN --network=none --mount=type=bind,source=dists,target=/tmp/dists \
    pip install --no-cache-dir --no-deps --require-hashes --no-index --find-links /tmp/dists \
    -r /tmp/build/requirements.txt
COPY wheels /tmp/build/wheels
RUN pip install --no-cache-dir --no-deps --no-index /tmp/build/wheels/*.whl && pip check && rm -rf /tmp/build
ENV WFT_BUILD_ID=${WFT_BUILD_ID} PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python", "-m", "wft_workflow.service"]
