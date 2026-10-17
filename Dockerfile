FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY health_sync ./health_sync
RUN pip install --no-cache-dir .
ENTRYPOINT ["python", "-m", "health_sync"]
