FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y \
	build-essential \
	libgomp1 \
	portaudio19-dev \
	libasound2-dev \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]