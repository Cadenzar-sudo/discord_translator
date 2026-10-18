FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# GPU build: docker build --build-arg CUDA=gpu (needs the nvidia container
# runtime at `docker run` — see compose.gpu.yml). CT2 finds cuBLAS/cuDNN via
# LD_LIBRARY_PATH; the driver comes from the host.
ARG CUDA=cpu
RUN if [ "$CUDA" = "gpu" ]; then pip install --no-cache-dir \
      nvidia-cublas-cu12 nvidia-cudnn-cu12; fi
ENV LD_LIBRARY_PATH="/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib"

COPY *.py ./

ENV PYTHONUNBUFFERED=1 \
    TRANSLATOR_MODELS_DIR=/models \
    TRANSLATOR_CONFIG=/data/config.json

VOLUME ["/models", "/data"]
EXPOSE 8765
ENTRYPOINT ["python", "-u", "main.py"]
