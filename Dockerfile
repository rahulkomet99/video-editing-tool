# Streamlit UI + full render stack in one image.
FROM python:3.10-slim

# ffmpeg (with libass/libfreetype for captions) + a bold system font.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so this layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + config + trend fallback data (no user assets baked in).
COPY src/ ./src/
COPY ui/ ./ui/
COPY config.yaml ./
COPY data/ ./data/

# Writable HF cache for the auto-caption model download; unbuffered logs.
ENV HF_HOME=/app/.cache/huggingface \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8501

# Provide the key at runtime, e.g.:
#   docker run -p 8501:8501 -e ANTHROPIC_API_KEY=sk-ant-... \
#     -v "$PWD/assets:/app/assets" -v "$PWD/output:/app/output" <image>
CMD ["streamlit", "run", "ui/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
