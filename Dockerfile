FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8765

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY public ./public
COPY tooltime ./tooltime
COPY PS1 ./PS1

RUN useradd --create-home --uid 10001 tooltime \
    && mkdir -p /app/.runtime \
    && chown -R tooltime:tooltime /app/.runtime
USER tooltime

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8765') + '/', timeout=4)"

CMD ["python", "server.py", "--host", "0.0.0.0"]
