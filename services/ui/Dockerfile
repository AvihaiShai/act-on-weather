FROM python:3.12-slim

WORKDIR /app
COPY services/ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ui/ /app/
COPY data/ /app/data/

# Non-root. The healthcheck uses python itself: python:3.12-slim ships neither
# curl nor wget, and adding a package just to probe a local port is waste.
RUN useradd -u 10001 -m appuser && chown -R appuser /app
USER 10001

EXPOSE 8501
HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=6 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health',timeout=3).status==200 else 1)"

CMD ["streamlit", "run", "app.py"]
