"""Extractors for rtfm plugin."""

from rtfm.extractors import (
    code_extractor,
    config_extractor,
    crossref_extractor,
    doc_extractor,
    domain_extractor,
)

__all__ = [
    "code_extractor",
    "config_extractor",
    "crossref_extractor",
    "doc_extractor",
    "domain_extractor",
]

try:
    from rtfm.extractors import typescript_extractor
    __all__.append("typescript_extractor")
except ImportError:
    pass
