FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Render 등 대부분의 무료 PaaS는 PORT 환경변수로 포트를 지정하고 컨테이너가
# 0.0.0.0에 바인딩할 것을 요구함.
ENV PORT=8420
EXPOSE 8420
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
