"""FastAPI REST API for CodeKB — serves the Cloudscape Web UI."""

# Load .env into os.environ as the very first step
from ..core.config import _load_dotenv_once

_load_dotenv_once()
