FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000

# Migrations run at boot rather than from a developer's laptop: the database
# accepts connections only from inside Render, so there is nowhere else to run
# them from. `alembic upgrade head` is idempotent, and the service runs a single
# instance, so there is no concurrent-migration race to worry about. If a
# migration fails the container exits rather than serving against a schema it
# does not understand.
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh
CMD ["./docker-entrypoint.sh"]
