# Production server: gunicorn with threaded workers, so each long SSE stream (a generation
# runs 25-135 s) holds one thread, not a whole worker. Start: gunicorn -c gunicorn.conf.py
#
# Not gevent: its monkey-patched ssl breaks the Anthropic SDK's TLS (truststore), and the
# pipeline already runs its parallel calls on ordinary threads.
import os

wsgi_app = "explainer.app:create_app()"
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = "gthread"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("THREADS", "32"))  # concurrent requests (and open streams) per worker
# The worker's main thread sends the heartbeat, so a long stream never trips this.
timeout = 300
graceful_timeout = 60
accesslog = "-"
