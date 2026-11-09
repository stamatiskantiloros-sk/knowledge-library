"""Importing this package registers every ingestor. To add a source type, write a module with an
@register'd Ingestor subclass and import it here; nothing else changes."""
from . import images, media, office, pdf, text, web  # noqa: F401
from .base import Context, Ingestor, IngestResult, InputItem, find_ingestor, register, registry

__all__ = ["Context", "Ingestor", "IngestResult", "InputItem", "find_ingestor", "register", "registry"]
