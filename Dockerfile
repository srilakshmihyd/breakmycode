FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 breakmycode

WORKDIR /app

COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY breakmycode ./breakmycode
COPY demo_repo ./demo_repo
COPY evals ./evals

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && chown -R breakmycode:breakmycode /app

USER breakmycode

CMD ["breakmycode", "eval", "/app/evals/cases.json", "--verify-patches", "--output", "/tmp/eval-report.json"]
