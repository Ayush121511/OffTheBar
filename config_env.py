import os
from dotenv import load_dotenv

# Explicit path for PythonAnywhere safety
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_PATH)


def require_env(name: str) -> str:
    value = os.getenv(name)

    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")

    return value.strip()