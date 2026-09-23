FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[analysis]"

RUN useradd --system --uid 10001 --no-create-home collector \
    && mkdir /reports && chown collector /reports
USER collector

ENTRYPOINT ["emt-collector"]
CMD ["run"]
