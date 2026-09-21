# Cloud Run image for the Off The Bar Flask backend.
#
# Includes Playwright/Chromium (crawl4ai's scraper needs a real headless
# browser, which is why this can't run on most serverless-JS platforms) and
# NLTK's punkt tokenizer data, both otherwise only documented as manual local
# setup steps in CLAUDE.md.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NLTK_DATA=/usr/local/share/nltk_data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --with-deps pulls in the apt packages Chromium needs (fonts, libnss3, etc.)
RUN python -m playwright install --with-deps chromium

RUN python -m nltk.downloader -d "$NLTK_DATA" punkt punkt_tab

COPY . .

# Cloud Run injects PORT; gunicorn binds to it. Single worker with several
# threads suits the streaming Gemini responses (long-lived connections,
# mostly I/O-bound waiting on Gemini/Pinecone/SearXNG) better than multiple
# worker processes, and sidesteps the chat_storage.py concurrent-write caveat
# noted in CLAUDE.md's Future work.
ENV PORT=8080
EXPOSE 8080
CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 0 run:app
