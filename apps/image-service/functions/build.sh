#!/usr/bin/env bash
# Packages the Azure and GCP function folders (run from apps/image-service, e.g. in GitHub Actions or Cloud Shell).
# Lambda uses its own Dockerfile instead.
#   ./functions/build.sh            -> dist/azure/ and dist/gcp/ ready to deploy
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf dist && mkdir -p dist
for target in azure gcp; do
  mkdir -p "dist/$target"
  cp -r "functions/$target/." "dist/$target/"
  cp -r imgsvc "dist/$target/imgsvc"
  python scripts/download_model.py "dist/$target/models"
done
echo "Deploy with:"
echo "  Azure: cd dist/azure && func azure functionapp publish <app-name> --python"
echo "  GCP:   cd dist/gcp && gcloud functions deploy classify --gen2 --runtime python311 --entry-point classify --trigger-http --memory 1Gi --region <region> --source ."
echo "         (repeat with resize / --entry-point resize)"
