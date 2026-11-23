# Lambda container image (push to ECR — Lambda cannot pull from GHCR).
# Build from apps/image-service:
#   docker build -f functions/aws_lambda/Dockerfile -t imgsvc-lambda .
# Create two functions from the same image with env ROLE=resizer and ROLE=classifier.
FROM public.ecr.aws/lambda/python:3.11

COPY requirements-core.txt ./
RUN pip install --no-cache-dir -r requirements-core.txt --target "${LAMBDA_TASK_ROOT}"

COPY scripts/download_model.py /tmp/
RUN python /tmp/download_model.py /opt/models

COPY imgsvc ${LAMBDA_TASK_ROOT}/imgsvc
COPY functions/aws_lambda/handler.py ${LAMBDA_TASK_ROOT}/

ENV MODEL_PATH=/opt/models/mobilenetv2-12.onnx \
    LABELS_PATH=/opt/models/imagenet_classes.txt \
    ORT_THREADS=1

CMD ["handler.handler"]
