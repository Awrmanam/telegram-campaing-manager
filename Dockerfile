FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir . && mkdir -p /data/database /data/sessions /data/media && chown -R app:app /data
USER app
VOLUME ["/data/database", "/data/sessions", "/data/media"]
CMD ["python", "-m", "app.main"]
