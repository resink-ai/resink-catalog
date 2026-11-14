# resink-catalog image. Build from the repository root: docker build -t resink-catalog .
# Run it with the port published on the host's loopback only, and your data mounted:
#   docker run --rm -p 127.0.0.1:8765:8765 -v "$HOME/.resink-catalog:/data" -v "$HOME/exports:/exports:ro" resink-catalog
# The access token is printed on the first line of the log.

FROM node:22-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv==0.10.7
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
COPY --from=web /src/src/resink_catalog/static src/resink_catalog/static
RUN uv build --wheel

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 catalog && mkdir /data && chown catalog /data
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER catalog
ENV RESINK_CATALOG_HOME=/data RESINK_CATALOG_IN_CONTAINER=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/healthz')"
ENTRYPOINT ["resink-catalog"]
CMD ["serve", "--no-browser"]
