from __future__ import annotations


class CatalogError(Exception):
    """Catalog build or validation failure. The message names the file or slug."""
