FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY apexfabric /app/apexfabric
USER 65532:65532
ENTRYPOINT ["python3", "-m", "apexfabric.node_management.status_controller.controller"]
