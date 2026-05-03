FROM python:3.11-slim

WORKDIR /app
COPY . /app

ENV HOST=0.0.0.0
ENV PORT=8020

EXPOSE 8020
CMD ["python", "-B", "scripts/serve_online_system.py", "--host", "0.0.0.0"]
