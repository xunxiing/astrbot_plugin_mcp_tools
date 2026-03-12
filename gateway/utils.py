from __future__ import annotations

import dataclasses
import datetime as dt
from enum import Enum
from typing import Any

from astrbot.core.message.components import (
    At,
    AtAll,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.core.message.message_event_result import MessageChain


class GatewayError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def get_by_dotpath(data: dict[str, Any], path: str | None) -> Any:
    if not path:
        return data
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_by_dotpath(
    data: dict[str, Any],
    path: str,
    value: Any,
    *,
    create_missing: bool,
) -> None:
    if not path:
        raise GatewayError("path must not be empty")

    cur: Any = data
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            raise GatewayError(f"non-dict node at {part}")
        if part not in cur:
            if not create_missing:
                raise GatewayError(f"path not found: {path}", status_code=404)
            cur[part] = {}
        cur = cur[part]

    if not isinstance(cur, dict):
        raise GatewayError(f"non-dict parent for {parts[-1]}")
    cur[parts[-1]] = value


def build_message_chain(
    message: str | None,
    parts: list[dict[str, Any]] | None,
) -> MessageChain:
    chain = MessageChain()
    if message:
        chain.chain.append(Plain(message))
    if not parts:
        return chain

    for part in parts:
        if not isinstance(part, dict):
            continue

        part_type = str(part.get("type", "")).lower()
        if part_type in ("plain", "text"):
            text = part.get("text")
            if text is not None:
                chain.chain.append(Plain(str(text)))
        elif part_type == "image":
            url = part.get("url")
            file_path = part.get("file_path")
            if url:
                chain.chain.append(Image.fromURL(str(url)))
            elif file_path:
                chain.chain.append(Image.fromFileSystem(str(file_path)))
        elif part_type == "file":
            name = str(part.get("name") or "file")
            url = part.get("url")
            file_path = part.get("file_path")
            if url:
                chain.chain.append(File(name=name, url=str(url)))
            elif file_path:
                chain.chain.append(File(name=name, file=str(file_path)))
        elif part_type == "record":
            url = part.get("url")
            file_path = part.get("file_path")
            if url:
                chain.chain.append(Record.fromURL(str(url)))
            elif file_path:
                chain.chain.append(Record.fromFileSystem(str(file_path)))
        elif part_type == "video":
            url = part.get("url")
            file_path = part.get("file_path")
            if url:
                chain.chain.append(Video.fromURL(str(url)))
            elif file_path:
                chain.chain.append(Video.fromFileSystem(str(file_path)))
        elif part_type == "at":
            qq = part.get("qq")
            name = part.get("name")
            if qq is not None:
                chain.chain.append(At(qq=str(qq), name=str(name or "")))
        elif part_type in ("at_all", "atall"):
            chain.chain.append(AtAll())
        elif part_type == "reply":
            message_id = part.get("message_id") or part.get("id")
            if message_id is not None:
                chain.chain.append(Reply(id=str(message_id)))

    return chain


def sanitize_message_parts_for_storage(
    parts: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not parts:
        return []
    sanitized: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        sanitized.append({k: v for k, v in part.items() if k != "path"})
    return sanitized


def to_plain_message_parts(
    message: str | None,
    parts: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if parts:
        return sanitize_message_parts_for_storage(parts)
    if message:
        return [{"type": "plain", "text": message}]
    return []


def serialize_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return {
            key: serialize_value(val) for key, val in dataclasses.asdict(value).items()
        }
    if hasattr(value, "model_dump"):
        try:
            return serialize_value(value.model_dump())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: serialize_value(val)
            for key, val in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


def ensure_mapping(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayError(message)
    return value
