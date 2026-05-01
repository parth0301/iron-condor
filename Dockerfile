FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY iron_condor_live.py .

CMD ["python", "iron_condor_live.py"]
