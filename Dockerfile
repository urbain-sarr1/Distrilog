# UN SEUL Dockerfile pour les 4 services : ARG SERVICE choisit le main.py
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG SERVICE
COPY ${SERVICE}/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
