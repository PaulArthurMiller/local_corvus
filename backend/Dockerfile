FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; r = urllib.request.urlopen('http://localhost:8000/health', timeout=8); import json; d = json.loads(r.read()); sys.exit(0 if d.get('db') == 'ok' else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]