# Builds python-qpid-proton from its sdist in a build stage on the pinned python image, and installs
# only the wheel in the runtime stage, which has no compiler.
ARG BASE
FROM ${BASE} AS build
ARG PROTON
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip wheel --no-cache-dir --no-binary python-qpid-proton -w /wheels "python-qpid-proton==${PROTON}"

FROM ${BASE}
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl \
    && python -c "import proton, sys; print('proton', proton.VERSION, 'python', sys.version.split()[0])"
