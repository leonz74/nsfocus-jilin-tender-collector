from __future__ import annotations

from .base import SourceAdapter
from .ccgp_archive import CCGPArchiveSource
from .ccgp_search import CCGPSearchSource
from .custom_web import CustomWebSource
from .okcis import OKCISSource
from .jilin_ggzy import JilinGGZYSource
from .national_ggzy import NationalGGZYSource
from .url_seed import URLSeedSource


def source_display_name(config: dict) -> str:
    mapping = {"jilin_ggzy": JilinGGZYSource, "national_ggzy": NationalGGZYSource,
               "ccgp_archive": CCGPArchiveSource, "ccgp_search": CCGPSearchSource,
               "url_seed": URLSeedSource, "custom_web": CustomWebSource}
    return str(config.get("name") or mapping[config["type"]].display_name)


def build_sources(client, source_configs: list[dict], base_dir=None) -> list[SourceAdapter]:
    mapping = {
        "jilin_ggzy": JilinGGZYSource,
        "national_ggzy": NationalGGZYSource,
        "ccgp_archive": CCGPArchiveSource,
        "ccgp_search": CCGPSearchSource,
        "url_seed": URLSeedSource,
        "custom_web": CustomWebSource,
    }
    sources: list[SourceAdapter] = []
    for original_config in source_configs:
        config = dict(original_config)
        if base_dir is not None:
            config["_base_dir"] = str(base_dir)
        if not config.get("enabled", True):
            continue
        source_type = config.get("type")
        if source_type not in mapping:
            raise ValueError(f"未知数据源类型: {source_type}")
        adapter = OKCISSource if source_type == "custom_web" and config.get("adapter") == "okcis" else mapping[source_type]
        sources.append(adapter(client, config))
    return sorted(sources, key=lambda source: source.authority_rank, reverse=True)


__all__ = ["SourceAdapter", "CustomWebSource", "build_sources"]
