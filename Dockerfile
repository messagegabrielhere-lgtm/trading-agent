# Always-on trading agent (agent/live.py). Keep the state file on a persistent volume at /data.
FROM python:3.12-slim
WORKDIR /app
COPY requirements-live.txt .
RUN pip install --no-cache-dir -r requirements-live.txt
COPY agent agent
COPY config.json .
ENV LIVE_STATE=/data/live_state.json PYTHONUNBUFFERED=1
CMD ["python", "-m", "agent.live"]
