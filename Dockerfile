# FoxProfile hub: panel, users and profile storage. No browser runs here; each
# operator's PC runs `python -m src.agent` (see docs/HUB.md).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements-hub.txt ./
# The desktop UI (flet) is not needed on the hub; requirements-hub.txt is (installers
# in an S3 bucket). requirements.txt stays: agents compare theirs with it.
RUN grep -v '^flet' requirements.txt > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt -r requirements-hub.txt \
    && rm /tmp/req.txt

COPY src ./src
COPY chrome_engine ./chrome_engine

ENV FOXPROFILE_HUB=true \
    FOXPROFILE_API_HOST=0.0.0.0 \
    FOXPROFILE_API_PORT=8000 \
    FOXPROFILE_PROFILES_FILE=/data/profiles.json \
    FOXPROFILE_ACCOUNTS_FILE=/data/accounts.json \
    FOXPROFILE_EXTENSIONS_FILE=/data/extensions.json \
    FOXPROFILE_EXTENSIONS_DIR=/data/extensions \
    FOXPROFILE_DATA_DIR=/data/camoufox_data \
    FOXPROFILE_HUB_FILE=/data/hub.json \
    FOXPROFILE_LOG_DIR=/data/logs \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8000
HEALTHCHECK CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ['FOXPROFILE_API_PORT'])"
CMD ["python", "-m", "src.server"]
