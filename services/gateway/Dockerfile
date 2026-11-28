FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py store.py guard.py public_profile.py availability.py notify.py identity.py outbound.py ./
COPY profile.example.md triage_agent.md outbound_policy.example.yaml ./

# Queue + audit log live here; mount a volume so they survive redeploys.
RUN mkdir -p /data
ENV GATEWAY_DB_PATH=/data/gateway.db

CMD ["python", "server.py"]
