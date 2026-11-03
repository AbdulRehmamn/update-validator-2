FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html nanpa_prefixes.sqlite3 update_offline_database.py ./
ENV HOST=0.0.0.0 PORT=8000 NO_BROWSER=1
EXPOSE 8000
# One worker keeps the portal token and live-lookup cache consistent. The app
# is async, so a single worker still handles many requests concurrently.
# Pass secrets at runtime, never in the image:  docker run --env-file .env ...
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
