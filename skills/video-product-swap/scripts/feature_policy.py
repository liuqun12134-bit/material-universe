"""Retired creation routes remain readable for paid task recovery."""
RETIRED_KINDS = {"full", "lipsync", "repair"}
RETIRED_MODELS = {"sync3", "sync_lipsync", "flashvsr", "flashvsr_upscale", "kaiyuncode-omni_video_edit"}


def retired(kind, config):
    return kind in RETIRED_KINDS or config.get("model") in RETIRED_MODELS


def require_creation(kind, config):
    if retired(kind, config):
        raise ValueError("此功能已下线，历史任务仅支持查看、查询和下载。请使用图音生视频。")
