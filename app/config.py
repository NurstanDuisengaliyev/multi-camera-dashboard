"""Load settings from config.yaml and set up logging."""
import logging
import os

import yaml

# The config file path can be changed with an environment variable (useful in Docker).
CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")
LOG_DIR = os.environ.get("LOG_DIR", "logs")


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def setup_logging(log_file="app.log"):
    """Print logs to the terminal AND save them to logs/<log_file> (needed for submission)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(LOG_DIR, log_file)),
        ],
    )
