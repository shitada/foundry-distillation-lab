FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
COPY LICENSE .
RUN pip install --no-cache-dir -r requirements.txt
COPY foundry_distillation_lab ./foundry_distillation_lab
COPY main.py capture.py tools.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "main.py"]
