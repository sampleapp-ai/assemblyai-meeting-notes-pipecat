FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY meeting_notes.py .
COPY run.py .
COPY client/ ./client/

EXPOSE 7860

CMD ["python", "meeting_notes.py", "--host", "0.0.0.0", "--port", "7860"]
