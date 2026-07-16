FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mapgen.py server.py regen_child.py how.html ./

CMD ["python", "server.py"]
