FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
WORKDIR /ui
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN useradd --create-home --uid 10001 app && chown -R app:app /ui
USER app
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
