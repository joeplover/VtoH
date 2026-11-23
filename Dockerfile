FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
USER 65532:65532
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8765", "--ws-max-size", "32768", "--no-access-log", "--log-level", "warning"]
