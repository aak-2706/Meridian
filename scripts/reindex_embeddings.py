from __future__ import annotations

import json

from meridian.config import settings
from meridian.store import Store


if __name__ == "__main__":
    result = Store(settings.db_path).reindex_embeddings()
    print(json.dumps(result, indent=2))
