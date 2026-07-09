# Stage 1: build the React frontend
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY package.json .
RUN npm install
COPY public ./public
COPY src ./src
RUN npm run build

# Stage 2: run the FastAPI backend, serving the built frontend as static files
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY --from=frontend-build /frontend/build ./frontend/build
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
