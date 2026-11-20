FROM postgres:16-bookworm

# Install pg_hint_plan and HypoPG from the official PGDG repository
# (already configured in the official postgres:16-bookworm base image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-16-pg-hint-plan \
    postgresql-16-hypopg \
    && rm -rf /var/lib/apt/lists/*
