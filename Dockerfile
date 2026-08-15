FROM python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

LABEL org.opencontainers.image.source="https://github.com/tylerkolden/school-bells" \
      org.opencontainers.image.description="Safe local test environment for the School Bell System"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system bell \
    && adduser --system --ingroup bell --home /nonexistent --no-create-home bell

WORKDIR /app
COPY pyproject.toml README.md ./
COPY bell ./bell
RUN python -m pip install .

COPY --chown=bell:bell docker/config /var/lib/bell/config
COPY --chown=bell:bell sounds /var/lib/bell/sounds
RUN install -d -o bell -g bell /var/lib/bell/state /var/lib/bell/logs

USER bell
EXPOSE 8080 9000
VOLUME ["/var/lib/bell"]

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=6 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).read()"]

CMD ["python", "-m", "bell.service", "--config-dir", "/var/lib/bell/config"]
