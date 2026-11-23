# Container image for the Integration Fabric gateway (console + unified FHIR R4 API).
# The mock source systems are built into the image so the demo estate runs anywhere;
# in production the connectors point at the real systems instead.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fabric/ fabric/
COPY mock_systems/ mock_systems/
COPY generated/ generated/
COPY runs/ runs/
COPY systems.yaml .

# Build the Synthea estate the connectors were built and validated on (mock_systems/synthea_estate.json)
RUN python -m fabric seed

EXPOSE 8000
# --check-every: automatic drift / contract checks with alerts (set FABRIC_ALERT_WEBHOOK for Slack or Teams)
CMD ["python", "-m", "fabric", "gateway", "--host", "0.0.0.0", "--port", "8000", "--check-every", "15"]
