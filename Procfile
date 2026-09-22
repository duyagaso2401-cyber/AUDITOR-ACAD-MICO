web: gunicorn app:app --workers ${WEB_CONCURRENCY:-2} --threads 4 --worker-class gthread --timeout 180 --bind 0.0.0.0:$PORT --access-logfile -
