# resink-catalog image. Build from the repository root: docker build -t resink-catalog .
# Base images are pinned by digest and runtime dependencies are installed from uv.lock with hashes, so a rebuild
# gives the same packages we tested. Update the digests and uv.lock in a reviewed change.
#
# Run it as yourself, so the mounted directory (owned by you) is writable, with the port on the host's loopback only:
#   docker run --rm --user "$(id -u):$(id -g)" -p 127.0.0.1:8765:8765 \
#     -v "$HOME/.resink-catalog:/data" -v "$HOME/exports:/exports:ro" resink-catalog
# (Without --user the process runs as uid 10001; that works with a named volume, `-v resink:/data`, not with a host
# directory owned by someone else.) The access link is printed on the first line of the log.

FROM node:22-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
RUN pip install --no-cache-dir uv==0.10.7
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY --from=web /src/src/resink_catalog/static src/resink_catalog/static
RUN uv export --frozen --no-dev --no-emit-project --format requirements.txt -o /requirements.txt \
 && uv build --wheel

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN useradd --create-home --uid 10001 catalog && mkdir /data && chown catalog /data
COPY --from=build /requirements.txt /tmp/requirements.txt
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/*.whl && rm /tmp/*.whl /tmp/requirements.txt
USER catalog
# a --user other than 10001 has no home directory entry: keep anything that writes to $HOME out of "/"
ENV HOME=/tmp RESINK_CATALOG_HOME=/data RESINK_CATALOG_IN_CONTAINER=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/healthz')"
ENTRYPOINT ["resink-catalog"]
CMD ["serve", "--no-browser"]
