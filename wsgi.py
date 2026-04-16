"""WSGI entry point for production (Gunicorn)."""
from app import create_app

app = create_app()
