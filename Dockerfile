FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
# readable for whatever PUID the container is told to run as
RUN chmod -R a+rX /app

ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'], timeout=5)" || exit 1

CMD ["python", "entrypoint.py"]
