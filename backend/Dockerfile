FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY requirements-proxy.txt .
RUN pip install -r requirements-proxy.txt
COPY . .
RUN useradd --create-home appuser

FROM base AS test
RUN pip install -r requirements-test.txt
USER appuser

FROM base AS runtime
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
