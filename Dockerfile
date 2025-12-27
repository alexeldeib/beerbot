FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy application
COPY src/ ./src/

# Run with uvicorn
CMD ["uvicorn", "src.beerbot.main:app", "--host", "0.0.0.0", "--port", "8080"]
