FROM python:3.12-slim

ARG REPORTER_VERSION=0.1.0
ENV REPORTER_VERSION=${REPORTER_VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends vainfo intel-media-va-driver \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY apexfabric /app/apexfabric
COPY deploy/config/hardware-profiles.json /app/config/hardware-profiles.json
USER 65532:65532
EXPOSE 9100
ENTRYPOINT ["python3", "-m", "apexfabric.node_management.reporter.reporter"]
