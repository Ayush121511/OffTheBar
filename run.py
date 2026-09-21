import os
from pathlib import Path
from json import load
import config_env

BASE_DIR = Path(__file__).resolve().parent


def _load_local_env_files() -> None:
    """Load simple KEY=VALUE pairs from repo-local env helper files."""
    for env_path in (BASE_DIR / ".env.sh", BASE_DIR / ".env"):
        if not env_path.exists():
            continue

        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export ") :].strip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            os.environ.setdefault(key, value)


_load_local_env_files()

from server.app import app
from server.website import Website
from server.backend import Backend_Api

CONFIG_PATH = Path(os.getenv("OFFTHEBAR_CONFIG_PATH", BASE_DIR / "config.json"))

with CONFIG_PATH.open("r") as config_file:
    config = load(config_file)

site_config = config['site_config']

# Local dev convenience: FLASK_DEBUG in .env enables Flask's auto-reloader
# (restarts on code changes) without touching the shared config.json, which
# is also read in production — debug mode must never be on there.
_debug_override = os.getenv("FLASK_DEBUG")
if _debug_override is not None:
    site_config['debug'] = _debug_override.strip().lower() in ("1", "true", "yes")

site = Website(app)
for route in site.routes:
    app.add_url_rule(
        route,
        view_func = site.routes[route]['function'],
        methods   = site.routes[route]['methods'],
    )

backend_api  = Backend_Api(app, config)
for route in backend_api.routes:
    app.add_url_rule(
        route,
        view_func = backend_api.routes[route]['function'],
        methods   = backend_api.routes[route]['methods'],
    )

if __name__ == '__main__':
    print(f"Running on port {site_config['port']}")
    app.run(**site_config)
    print(f"Closing port {site_config['port']}")

print(app)
