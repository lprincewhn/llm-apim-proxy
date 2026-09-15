FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt ./
RUN mkdir .build && TMPDIR=/app/.build pip install --no-cache-dir -r requirements.txt && rm -rf .build
COPY app.py executor.py faults.py ./

USER 10001:10001
EXPOSE 8000
CMD ["python", "app.py"]
