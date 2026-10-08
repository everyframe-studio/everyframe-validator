FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/validator
LABEL org.opencontainers.image.source="https://github.com/everyframe-studios/everyframe-validator" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --gid 10001 validator && useradd --uid 10001 --gid 10001 --create-home validator
WORKDIR /app
COPY pyproject.toml README.md LICENSE LICENSE-NOTICE.md ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /state && chown validator:validator /state && chmod 700 /state
USER 10001:10001
ENTRYPOINT ["everyframe-validator"]
CMD ["--help"]
