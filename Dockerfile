FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Force entrypoint to just run our script, ignoring any template bootstrap scripts
ENTRYPOINT ["python", "main.py"]
