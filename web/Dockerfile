FROM python:3.13-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY routing ./routing
COPY templates ./templates
COPY static ./static

ENV PYTHONUNBUFFERED=1
ENV PORT=5000
ENV GTFS_PATH=/otp-data/moscow-gtfs.zip
EXPOSE 5000

CMD ["python", "app.py"]
