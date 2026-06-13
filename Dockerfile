# Layer 5 — Runtime: containerized boundary.
# Fixed, reproducible, no ambient trust for the untrusted agents.
FROM python:3.11-slim

# Non-root user: agents never execute as root inside the boundary.
RUN useradd --create-home --uid 1000 harness
WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Persisted state (checkpoints + records) lives here; mount a volume to keep it.
RUN mkdir -p /app/runs && chown -R harness:harness /app
USER harness

# No network egress is required for the seeded demo — the agents read from
# ./harness/data/seed. Run with `--network none` to prove zero ambient trust:
#   docker run --rm --network none gittrippin run --user demo --trip trip.json
ENTRYPOINT ["python", "main.py"]
CMD ["run", "--trip", "trip.json"]
