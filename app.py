"""REACH — standalone application entry point.

REACH is its own product. It shares no process, no database and no catalog with
anything else in this repository: this file builds a Flask app that mounts the
REACH blueprint and nothing else.

    pip install -r requirements.txt
    python app.py            # http://127.0.0.1:5000

REACH runs with no configuration. Without a search credential, discovery uses
the built-in fixture corpus and every screen says so. See
docs/INTEGRATION_SETUP.md to connect real providers.
"""

import hashlib
import os
from datetime import timedelta

from flask import Flask, render_template

from reach import config
from reach.web import bp as reach_bp


def create_app():
    app = Flask(__name__)
    # Sessions exist only for the access gate. The signing secret derives from
    # secrets the deployment already has, so an unlocked browser survives
    # restarts; with no secrets configured there is no gate to remember, and a
    # per-process key is fine.
    seed = config.env("REACH_ENCRYPTION_KEY", "") or config.env("REACH_ACCESS_KEY", "")
    app.secret_key = (hashlib.sha256(("reach-session::" + seed).encode("utf-8")).digest()
                      if seed else os.urandom(32))
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.register_blueprint(reach_bp)

    @app.route("/")
    def index():
        # The public front door. The app itself lives at /reach.
        return render_template("landing.html")

    @app.route("/healthz")
    def healthz():
        # Deliberately does not touch the database: a health check that fails on
        # a slow query takes the service down for a problem it did not have.
        return {"ok": True, "service": "reach"}

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
