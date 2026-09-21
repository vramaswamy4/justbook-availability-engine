"""Minimal application shell for the extracted availability engine.

In production this package is a ~100k-line Flask application. Here it is the smallest
thing that lets `app/utils/availability_engine.py` run UNCHANGED: a Flask-SQLAlchemy
`db` handle at the import path the engine expects (`from app import db`) and an app
factory pointed at a database URL.

Everything in this file is scaffolding written for this repository. The production
code is in `app/utils/`.
"""
import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def create_app(database_url=None):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        database_url or os.environ.get("DATABASE_URL") or "sqlite:///:memory:"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    from app.models import models  # noqa: F401  (registers the tables)
    return app
