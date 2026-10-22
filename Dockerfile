# Container for any host that runs Docker (Hugging Face Spaces, Render, Railway, Azure, ...).
#   docker build -t sap-agents .
#   docker run -p 8501:8501 sap-agents      -> http://localhost:8501
# The six mock APIs run inside the same container on localhost 8101-8106; only 8501 is exposed.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PORT=8501
EXPOSE 8501
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:' + __import__('os').environ['PORT'] + '/_stcore/health')"
CMD ["sh", "-c", "streamlit run dashboard/app.py --server.address 0.0.0.0 --server.port ${PORT}"]
