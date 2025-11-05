FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Chromium & deps đã có sẵn trong image này
CMD ["python", "main.py"]
