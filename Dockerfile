FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        procps \
        net-tools \
        iproute2 \
        bind9-dnsutils \
        netcat-openbsd \
        lsof \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 vless \
    && useradd --system --uid 10001 --gid vless \
        --home-dir /app --no-create-home --shell /usr/sbin/nologin vless

WORKDIR /app

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY vs.py /app/vs.py
COPY healthcheck.py /app/healthcheck.py

RUN chmod 0555 /usr/local/bin/docker-entrypoint.sh /app/vs.py /app/healthcheck.py

USER vless

EXPOSE 10086
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
