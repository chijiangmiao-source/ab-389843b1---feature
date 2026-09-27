FROM python:3.12-slim

WORKDIR /app

COPY app ./app
COPY tests ./tests
COPY verify ./verify

ENV PORT=8080 \
    AUDIT_DB=/data/audits.db

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=12 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=2)"

# Default command runs the audit API service; the compose `verify` service
# overrides this with the one-shot acceptance entrypoint.
CMD ["python3", "-m", "app.service"]
