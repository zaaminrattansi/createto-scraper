import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

USER_AGENT = "createto-scraper/0.1 (+https://github.com/zaaminrattansi/createto-scraper)"

SITE_HOSTS = {"createto.ca", "www.createto.ca"}
FILE_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".zip"}
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".mp4", ".mov", ".webm"}


def five_years_ago():
    today = date.today()
    return today.replace(year=today.year - 5, day=min(today.day, 28))


@dataclass
class Config:
    data_dir: Path = Path("data")
    start_url: str = "https://createto.ca/"
    since: date = field(default_factory=five_years_ago)

    site_delay: float = 1.0  # seconds between requests to createto.ca
    file_delay: float = 0.34  # file hosts are CDNs, ~3 req/s is fine
    timeout: float = 60
    max_file_mb: int = 100

    model: str = os.getenv("CREATETO_MODEL", "anthropic/claude-haiku-5.5")
    api_key: str | None = os.getenv("OPENROUTER_API_KEY")
    use_llm: bool = True
    llm_workers: int = 8
    max_llm_docs: int | None = None
    max_pages_per_doc: int = 60

    # replay = serve responses from a saved fixture dir instead of the network
    replay_dir: Path | None = None
    follow_links: bool = True

    @property
    def db_path(self):
        return self.data_dir / "createto.db"

    @property
    def blob_dir(self):
        return self.data_dir / "blobs"
