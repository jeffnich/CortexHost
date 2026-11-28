# Stage 1: build the owner dashboard (static files only; node is not in the final image).
FROM node:22-slim AS dashboard
WORKDIR /dash
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py store.py guard.py public_profile.py availability.py notify.py identity.py outbound.py dashboard.py ./
COPY profile.example.md triage_agent.md outbound_policy.example.yaml ./
COPY --from=dashboard /dash/dist ./dashboard/dist

# Queue + audit log live here; mount a volume so they survive redeploys.
RUN mkdir -p /data
ENV GATEWAY_DB_PATH=/data/gateway.db

CMD ["python", "server.py"]
