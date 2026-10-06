MY DOWNLOADER V9.4 CLOUD

1. Upload this folder to a GitHub repository.
2. In Render, create a Web Service from that repository.
3. Build command: pip install -r requirements.txt
4. Start command: gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 180 app:app
5. Add CLOUD_MODE=1.
6. After deploy, open /health.

Important: this cloud build does NOT read Chrome cookies. It is intended for public/accessible Instagram and Facebook media only. Private/login-required content is not bypassed.
