"""ASGI entry point: uvicorn fieldservice.main:app"""
from fieldservice.app import create_app

app = create_app()
