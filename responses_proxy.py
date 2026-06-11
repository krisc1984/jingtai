#!/usr/bin/env python3
"""
Minimal /v1/responses -> /v1/chat/completions proxy.

Purpose:
- Let Codex / GPT-5 Codex clients talk to an OpenAI-compatible upstream that
  only implements the classic chat completions protocol.
- Print readable logs for requests, upstream calls, and errors.

Notes:
- Implemented with Python stdlib only, so it can be started directly with
  `python responses_proxy.py`.
- Supports JSON responses and basic SSE streaming.
- Supports text messages, function tools, and function tool outputs.
- Built-in hosted tools from the official Responses API are not emulated here.
"""

from __future__ import annotations

import argparse
import cgi
import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple


def now_ts() -> int:
    return int(time.time())


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def iso_log_time() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def log(level: str, message: str, **fields: Any) -> None:
    parts = [f"[{iso_log_time()}]", level, message]
    if fields:
        suffix = " ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
        parts.append(suffix)
    print(" ".join(parts), flush=True)


def load_dotenv_file(path: str) -> None:
    if not os.path.exists(path):
        return
    loaded_keys: List[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value
            loaded_keys.append(key)
    if loaded_keys:
        log("INFO", "loaded .env file", path=path, keys=loaded_keys)


def short_json(data: Any, limit: int = 1000) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitize_filename(filename: str) -> str:
    name = os.path.basename(filename or "").strip().replace("\x00", "")
    if not name:
        return ""
    allowed = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            allowed.append(ch)
        else:
            allowed.append("_")
    cleaned = "".join(allowed).strip("._")
    return cleaned[:120]


def guess_extension(filename: str, content_type: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext:
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip().lower())
    if guessed == ".jpe":
        return ".jpg"
    return guessed or ".bin"


def build_public_origin(handler: BaseHTTPRequestHandler, configured_base_url: str) -> str:
    if configured_base_url:
        return configured_base_url.rstrip("/")
    proto = handler.headers.get("X-Forwarded-Proto") or ("https" if handler.headers.get("Forwarded", "").startswith("proto=https") else "http")
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "127.0.0.1"
    return f"{proto}://{host}"


def build_upload_response(handler: BaseHTTPRequestHandler, config: "ProxyConfig", saved_name: str, file_size: int) -> Dict[str, Any]:
    url = f"{build_public_origin(handler, config.public_base_url)}/uploads/{urllib.parse.quote(saved_name)}"
    return {
        "object": "image_upload",
        "filename": saved_name,
        "bytes": file_size,
        "url": url,
        "warning": "uploaded files are publicly accessible to anyone with the URL",
    }


def read_json_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json body: {exc}") from exc


def normalize_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in ("input_text", "output_text", "text"):
                parts.append(str(item.get("text", "")))
            elif item_type in ("input_image", "image_url"):
                image_url = item.get("image_url") or item.get("url") or ""
                detail = item.get("detail") or "auto"
                parts.append(f"[image: {image_url}, detail={detail}]")
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text", ""))
    return str(content)


def normalize_content_to_chat_parts(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return normalize_content_to_text(content)

    parts: List[Dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            parts.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": str(item.get("text", ""))})
        elif item_type == "input_image":
            image_url = item.get("image_url")
            if image_url:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url,
                            "detail": item.get("detail", "auto"),
                        },
                    }
                )
    if not parts:
        return ""
    return parts


def map_tool_choice(tool_choice: Any) -> Any:
    if tool_choice in (None, "auto", "none", "required"):
        return tool_choice
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice.get("name")
        if not name and isinstance(tool_choice.get("function"), dict):
            name = tool_choice["function"].get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return "auto"


def convert_response_input_to_chat_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        for item in instructions:
            messages.extend(convert_single_input_item(item, default_role="system"))

    input_data = payload.get("input")
    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            messages.extend(convert_single_input_item(item, default_role="user"))
    elif input_data is not None:
        messages.append({"role": "user", "content": normalize_content_to_text(input_data)})

    return messages


def convert_single_input_item(item: Any, default_role: str) -> List[Dict[str, Any]]:
    if isinstance(item, str):
        return [{"role": default_role, "content": item}]

    if not isinstance(item, dict):
        return [{"role": default_role, "content": normalize_content_to_text(item)}]

    item_type = item.get("type")

    if item_type in (None, "message"):
        role = item.get("role", default_role)
        content = normalize_content_to_chat_parts(item.get("content"))
        return [{"role": role, "content": content}]

    if item_type == "function_call_output":
        return [
            {
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or gen_id("call"),
                "content": normalize_content_to_text(item.get("output")),
            }
        ]

    if item_type == "function_call":
        call_id = item.get("call_id") or item.get("id") or gen_id("call")
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": item.get("name", "tool"),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }
                ],
            }
        ]

    if item_type in ("input_text", "output_text", "text"):
        return [{"role": default_role, "content": normalize_content_to_text(item)}]

    return [{"role": default_role, "content": normalize_content_to_text(item)}]


def convert_tools(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    tools = payload.get("tools") or []
    chat_tools: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function":
            chat_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", "tool"),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
        else:
            skipped.append(tool_type or "unknown")
    return chat_tools, skipped


def convert_response_request_to_chat_request(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    messages = convert_response_input_to_chat_messages(payload)
    tools, skipped_tools = convert_tools(payload)

    body: Dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": bool(payload.get("stream")),
    }

    passthrough_fields = [
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("presence_penalty", "presence_penalty"),
        ("frequency_penalty", "frequency_penalty"),
        ("user", "user"),
        ("metadata", "metadata"),
        ("parallel_tool_calls", "parallel_tool_calls"),
    ]
    for src, dst in passthrough_fields:
        if src in payload:
            body[dst] = payload[src]

    if "max_output_tokens" in payload:
        body["max_tokens"] = payload["max_output_tokens"]

    if tools:
        body["tools"] = tools
        tool_choice = map_tool_choice(payload.get("tool_choice", "auto"))
        if tool_choice is not None:
            body["tool_choice"] = tool_choice

    text_cfg = payload.get("text") or {}
    fmt = text_cfg.get("format") if isinstance(text_cfg, dict) else None
    if isinstance(fmt, dict):
        fmt_type = fmt.get("type")
        if fmt_type == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": fmt.get("name", "response"),
                    "schema": fmt.get("schema", {}),
                    "strict": bool(fmt.get("strict", False)),
                },
            }
        elif fmt_type == "json_object":
            body["response_format"] = {"type": "json_object"}

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and "effort" in reasoning:
        body["reasoning_effort"] = reasoning["effort"]

    return body, skipped_tools


def extract_usage(chat_response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    usage = chat_response.get("usage")
    if not isinstance(usage, dict):
        return None
    output_tokens = usage.get("completion_tokens", 0)
    prompt_tokens = usage.get("prompt_tokens", 0)
    reasoning_tokens = 0
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning_tokens = completion_details.get("reasoning_tokens", 0) or 0
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": usage.get("total_tokens", prompt_tokens + output_tokens),
    }


def convert_chat_response_to_responses(
    chat_response: Dict[str, Any],
    request_payload: Dict[str, Any],
    skipped_tools: List[str],
) -> Dict[str, Any]:
    created_at = chat_response.get("created", now_ts())
    response_id = gen_id("resp")
    output_items: List[Dict[str, Any]] = []

    choice = {}
    choices = chat_response.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
    message = choice.get("message", {}) if isinstance(choice, dict) else {}

    content = message.get("content")
    if isinstance(content, list):
        text_value = "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    else:
        text_value = content or ""

    if text_value:
        output_items.append(
            {
                "id": gen_id("msg"),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text_value,
                        "annotations": [],
                    }
                ],
            }
        )

    tool_calls = message.get("tool_calls") or []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        call_id = tool_call.get("id") or gen_id("call")
        output_items.append(
            {
                "id": gen_id("fc"),
                "type": "function_call",
                "call_id": call_id,
                "name": function.get("name", "tool"),
                "arguments": function.get("arguments", "{}"),
                "status": "completed",
            }
        )

    if not output_items:
        output_items.append(
            {
                "id": gen_id("msg"),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
        )

    resp = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": request_payload.get("instructions"),
        "max_output_tokens": request_payload.get("max_output_tokens"),
        "model": chat_response.get("model", request_payload.get("model")),
        "output": output_items,
        "parallel_tool_calls": bool(request_payload.get("parallel_tool_calls", True)),
        "previous_response_id": request_payload.get("previous_response_id"),
        "reasoning": {"effort": (request_payload.get("reasoning") or {}).get("effort"), "summary": None},
        "store": bool(request_payload.get("store", True)),
        "temperature": request_payload.get("temperature", 1.0),
        "text": request_payload.get("text", {"format": {"type": "text"}}),
        "tool_choice": request_payload.get("tool_choice", "auto"),
        "tools": request_payload.get("tools", []),
        "top_p": request_payload.get("top_p", 1.0),
        "truncation": request_payload.get("truncation", "disabled"),
        "usage": extract_usage(chat_response),
        "user": request_payload.get("user"),
        "metadata": request_payload.get("metadata", {}),
    }
    if skipped_tools:
        resp.setdefault("metadata", {})
        resp["metadata"]["proxy_skipped_tool_types"] = skipped_tools
    return resp


def sse_event(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def iter_sse_lines(stream: Any) -> Iterable[str]:
    for raw_line in stream:
        if not raw_line:
            continue
        try:
            yield raw_line.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            yield raw_line.decode("utf-8", errors="replace").rstrip("\r\n")


def build_stream_events_from_chat_chunk(
    chunk: Dict[str, Any],
    state: Dict[str, Any],
) -> List[Tuple[str, Dict[str, Any]]]:
    events: List[Tuple[str, Dict[str, Any]]] = []
    choices = chunk.get("choices") or []
    if not choices:
        return events

    delta = choices[0].get("delta") or {}
    finish_reason = choices[0].get("finish_reason")

    if not state.get("started"):
        state["started"] = True
        events.append(
            (
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": {
                        "id": state["response_id"],
                        "object": "response",
                        "created_at": state["created_at"],
                        "status": "in_progress",
                        "error": None,
                        "incomplete_details": None,
                        "instructions": state["request_payload"].get("instructions"),
                        "max_output_tokens": state["request_payload"].get("max_output_tokens"),
                        "model": chunk.get("model", state["request_payload"].get("model")),
                        "output": [],
                        "parallel_tool_calls": bool(state["request_payload"].get("parallel_tool_calls", True)),
                        "previous_response_id": state["request_payload"].get("previous_response_id"),
                        "reasoning": {"effort": (state["request_payload"].get("reasoning") or {}).get("effort"), "summary": None},
                        "store": bool(state["request_payload"].get("store", True)),
                        "temperature": state["request_payload"].get("temperature", 1.0),
                        "text": state["request_payload"].get("text", {"format": {"type": "text"}}),
                        "tool_choice": state["request_payload"].get("tool_choice", "auto"),
                        "tools": state["request_payload"].get("tools", []),
                        "top_p": state["request_payload"].get("top_p", 1.0),
                        "truncation": state["request_payload"].get("truncation", "disabled"),
                        "usage": None,
                        "user": state["request_payload"].get("user"),
                        "metadata": state["request_payload"].get("metadata", {}),
                    },
                },
            )
        )

    if "content" in delta and not state.get("message_started"):
        state["message_started"] = True
        events.append(
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": state["message_id"],
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            )
        )
        events.append(
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": state["message_id"],
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            )
        )

    if "content" in delta and delta.get("content"):
        text = delta["content"]
        state["message_text"] += text
        events.append(
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": state["message_id"],
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            )
        )

    for tool_call in delta.get("tool_calls", []) or []:
        index = tool_call.get("index", 0)
        current = state["tool_calls"].setdefault(
            index,
            {
                "call_id": gen_id("call"),
                "item_id": gen_id("fc"),
                "name": "",
                "arguments": "",
                "started": False,
            },
        )
        call_id = tool_call.get("id")
        if call_id:
            current["call_id"] = call_id
        function = tool_call.get("function") or {}
        if "name" in function and function["name"]:
            current["name"] = function["name"]
        if "arguments" in function and function["arguments"]:
            current["arguments"] += function["arguments"]

        if not current["started"]:
            current["started"] = True
            events.append(
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": state["next_output_index"],
                        "item": {
                            "id": current["item_id"],
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": current["call_id"],
                            "name": current["name"],
                            "arguments": current["arguments"],
                        },
                    },
                )
            )
            state["next_output_index"] += 1

    if finish_reason:
        if state.get("message_started"):
            events.append(
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": state["message_id"],
                        "output_index": 0,
                        "content_index": 0,
                        "text": state["message_text"],
                    },
                )
            )
            events.append(
                (
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": state["message_id"],
                        "output_index": 0,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": state["message_text"],
                            "annotations": [],
                        },
                    },
                )
            )
            events.append(
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": state["message_id"],
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": state["message_text"],
                                    "annotations": [],
                                }
                            ],
                        },
                    },
                )
            )

        ordered_tool_calls = sorted(state["tool_calls"].items(), key=lambda item: item[0])
        output_index = 1 if state.get("message_started") else 0
        output_items: List[Dict[str, Any]] = []
        if state.get("message_started"):
            output_items.append(
                {
                    "id": state["message_id"],
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": state["message_text"], "annotations": []}],
                }
            )

        for _, tool_state in ordered_tool_calls:
            item = {
                "id": tool_state["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": tool_state["call_id"],
                "name": tool_state["name"] or "tool",
                "arguments": tool_state["arguments"] or "{}",
            }
            events.append(
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    },
                )
            )
            output_items.append(item)
            output_index += 1

        final_response = {
            "id": state["response_id"],
            "object": "response",
            "created_at": state["created_at"],
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": state["request_payload"].get("instructions"),
            "max_output_tokens": state["request_payload"].get("max_output_tokens"),
            "model": chunk.get("model", state["request_payload"].get("model")),
            "output": output_items,
            "parallel_tool_calls": bool(state["request_payload"].get("parallel_tool_calls", True)),
            "previous_response_id": state["request_payload"].get("previous_response_id"),
            "reasoning": {"effort": (state["request_payload"].get("reasoning") or {}).get("effort"), "summary": None},
            "store": bool(state["request_payload"].get("store", True)),
            "temperature": state["request_payload"].get("temperature", 1.0),
            "text": state["request_payload"].get("text", {"format": {"type": "text"}}),
            "tool_choice": state["request_payload"].get("tool_choice", "auto"),
            "tools": state["request_payload"].get("tools", []),
            "top_p": state["request_payload"].get("top_p", 1.0),
            "truncation": state["request_payload"].get("truncation", "disabled"),
            "usage": None,
            "user": state["request_payload"].get("user"),
            "metadata": state["request_payload"].get("metadata", {}),
        }
        events.append(("response.completed", {"type": "response.completed", "response": final_response}))

    return events


class ProxyConfig:
    def __init__(
        self,
        upstream_base_url: str,
        upstream_api_key: str,
        timeout: int,
        upload_dir: str,
        public_base_url: str,
        max_upload_bytes: int,
    ) -> None:
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.upstream_api_key = upstream_api_key
        self.timeout = timeout
        self.upload_dir = upload_dir
        self.public_base_url = public_base_url.rstrip("/")
        self.max_upload_bytes = max_upload_bytes
        ensure_dir(self.upload_dir)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.upstream_base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.upstream_base_url}/models"


class ResponsesProxyHandler(BaseHTTPRequestHandler):
    server_version = "ResponsesProxy/0.1"
    protocol_version = "HTTP/1.1"
    config: ProxyConfig

    def log_message(self, fmt: str, *args: Any) -> None:
        log("HTTP", fmt % args)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        request_id = gen_id("req")
        if self.path in ("/health", "/healthz"):
            self.send_json(HTTPStatus.OK, {"ok": True, "service": "responses-proxy", "time": now_ts()})
            return
        if self.path.startswith("/uploads/"):
            self.serve_uploaded_file(request_id)
            return
        if self.path == "/v1/models":
            self.proxy_models(request_id)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": {"message": f"unknown path: {self.path}", "type": "not_found"}})

    def do_POST(self) -> None:
        request_id = gen_id("req")
        if self.path == "/v1/upload-image":
            self.handle_upload_image(request_id)
            return
        if self.path == "/v1/responses":
            self.handle_responses(request_id)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": {"message": f"unknown path: {self.path}", "type": "not_found"}})

    def send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_upstream_auth(self) -> str:
        inbound = self.headers.get("Authorization", "").strip()
        if inbound:
            return inbound
        if self.config.upstream_api_key:
            return f"Bearer {self.config.upstream_api_key}"
        return ""

    def proxy_models(self, request_id: str) -> None:
        url = self.config.models_url
        auth = self.get_upstream_auth()
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        log("INFO", "proxy models", request_id=request_id, url=url)
        req = urllib.request.Request(url=url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_common_headers()
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json; charset=utf-8"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            self.forward_http_error(exc, request_id)
        except Exception as exc:
            self.handle_internal_error(exc, request_id)

    def serve_uploaded_file(self, request_id: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        raw_name = parsed.path[len("/uploads/") :]
        file_name = sanitize_filename(urllib.parse.unquote(raw_name))
        if not file_name or file_name != raw_name:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "invalid upload path", "type": "invalid_request_error"}})
            return
        file_path = os.path.abspath(os.path.join(self.config.upload_dir, file_name))
        upload_root = os.path.abspath(self.config.upload_dir)
        if not file_path.startswith(upload_root + os.sep):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "invalid upload path", "type": "invalid_request_error"}})
            return
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "uploaded file not found", "type": "not_found"}})
            return
        with open(file_path, "rb") as fh:
            body = fh.read()
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        log("INFO", "serve uploaded file", request_id=request_id, file=file_name, bytes=len(body))
        self.send_response(HTTPStatus.OK)
        self.send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_upload_image(self, request_id: str) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "message": "expected multipart/form-data with a file field named file",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "empty upload body", "type": "invalid_request_error"}})
            return
        if content_length > self.config.max_upload_bytes:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": {
                        "message": f"upload too large: max {self.config.max_upload_bytes} bytes",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(content_length),
        }
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=environ)
        upload = form["file"] if "file" in form else (form["image"] if "image" in form else None)
        if upload is None or not getattr(upload, "file", None):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "missing file field", "type": "invalid_request_error"}})
            return

        original_name = sanitize_filename(getattr(upload, "filename", "") or "upload")
        if not original_name:
            original_name = "upload"
        ext = guess_extension(original_name, getattr(upload, "type", ""))
        saved_name = f"{int(time.time())}_{uuid.uuid4().hex[:12]}{ext}"
        file_path = os.path.join(self.config.upload_dir, saved_name)

        data = upload.file.read()
        if not data:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": "empty file", "type": "invalid_request_error"}})
            return
        if len(data) > self.config.max_upload_bytes:
            self.send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {
                    "error": {
                        "message": f"upload too large: max {self.config.max_upload_bytes} bytes",
                        "type": "invalid_request_error",
                    }
                },
            )
            return

        with open(file_path, "wb") as fh:
            fh.write(data)

        payload = build_upload_response(self, self.config, saved_name, len(data))
        log("INFO", "image uploaded", request_id=request_id, file=saved_name, bytes=len(data), url=payload["url"])
        self.send_json(HTTPStatus.OK, payload)

    def handle_responses(self, request_id: str) -> None:
        try:
            request_payload = read_json_body(self)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc), "type": "invalid_request_error"}})
            return

        log(
            "INFO",
            "incoming /v1/responses",
            request_id=request_id,
            stream=bool(request_payload.get("stream")),
            model=request_payload.get("model"),
            body=short_json(request_payload),
        )

        try:
            chat_payload, skipped_tools = convert_response_request_to_chat_request(request_payload)
            if not chat_payload.get("model"):
                raise ValueError("missing required field: model")
            if not chat_payload.get("messages"):
                raise ValueError("no usable input/messages found in request")

            log(
                "INFO",
                "translated request",
                request_id=request_id,
                skipped_tools=skipped_tools,
                upstream_body=short_json(chat_payload),
            )

            if chat_payload.get("stream"):
                self.handle_streaming_chat_proxy(request_id, request_payload, chat_payload)
            else:
                self.handle_non_stream_chat_proxy(request_id, request_payload, chat_payload, skipped_tools)
        except urllib.error.HTTPError as exc:
            self.forward_http_error(exc, request_id)
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": {"message": str(exc), "type": "invalid_request_error"}})
        except Exception as exc:
            self.handle_internal_error(exc, request_id)

    def build_upstream_request(self, url: str, payload: Dict[str, Any]) -> urllib.request.Request:
        auth = self.get_upstream_auth()
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        return urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def handle_non_stream_chat_proxy(
        self,
        request_id: str,
        request_payload: Dict[str, Any],
        chat_payload: Dict[str, Any],
        skipped_tools: List[str],
    ) -> None:
        req = self.build_upstream_request(self.config.chat_completions_url, chat_payload)
        started = time.time()
        with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
            raw = resp.read()
            chat_response = json.loads(raw.decode("utf-8"))
        took_ms = int((time.time() - started) * 1000)
        log("INFO", "upstream chat response", request_id=request_id, took_ms=took_ms, body=short_json(chat_response))
        response_payload = convert_chat_response_to_responses(chat_response, request_payload, skipped_tools)
        self.send_json(HTTPStatus.OK, response_payload)

    def handle_streaming_chat_proxy(
        self,
        request_id: str,
        request_payload: Dict[str, Any],
        chat_payload: Dict[str, Any],
    ) -> None:
        req = self.build_upstream_request(self.config.chat_completions_url, chat_payload)

        self.send_response(HTTPStatus.OK)
        self.send_common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        created_at = now_ts()
        state: Dict[str, Any] = {
            "response_id": gen_id("resp"),
            "message_id": gen_id("msg"),
            "created_at": created_at,
            "request_payload": request_payload,
            "message_text": "",
            "tool_calls": {},
            "started": False,
            "message_started": False,
            "next_output_index": 1,
        }

        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                for line in iter_sse_lines(resp):
                    if not line.startswith("data: "):
                        continue
                    data_text = line[6:]
                    if data_text == "[DONE]":
                        break
                    chunk = json.loads(data_text)
                    log("DEBUG", "upstream stream chunk", request_id=request_id, chunk=short_json(chunk, 600))
                    for event_name, event_data in build_stream_events_from_chat_chunk(chunk, state):
                        self.wfile.write(sse_event(event_name, event_data))
                        self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except BrokenPipeError:
            log("WARN", "client disconnected during stream", request_id=request_id)
        finally:
            took_ms = int((time.time() - started) * 1000)
            log("INFO", "stream finished", request_id=request_id, took_ms=took_ms)

    def forward_http_error(self, exc: urllib.error.HTTPError, request_id: str) -> None:
        body = exc.read()
        content_type = exc.headers.get("Content-Type", "application/json; charset=utf-8")
        log(
            "ERROR",
            "upstream http error",
            request_id=request_id,
            status=exc.code,
            body=body.decode("utf-8", errors="replace")[:2000],
        )
        self.send_response(exc.code)
        self.send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_internal_error(self, exc: Exception, request_id: str) -> None:
        log("ERROR", "internal proxy error", request_id=request_id, error=str(exc), trace=traceback.format_exc())
        payload = {
            "error": {
                "message": str(exc),
                "type": "proxy_error",
                "request_id": request_id,
            }
        }
        self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, payload)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Proxy /v1/responses to an upstream /v1/chat/completions server.")
    parser.add_argument("--host", default=os.getenv("PROXY_HOST", "0.0.0.0"), help="listen host")
    parser.add_argument("--port", type=int, default=int(os.getenv("PROXY_PORT", "8010")), help="listen port")
    parser.add_argument(
        "--upload-dir",
        default=os.getenv("UPLOAD_DIR", os.path.join(os.getcwd(), "uploads")),
        help="directory used to store uploaded files",
    )
    parser.add_argument(
        "--public-base-url",
        default=os.getenv("PUBLIC_BASE_URL", ""),
        help="public origin to use in returned upload URLs, e.g. https://example.com",
    )
    parser.add_argument(
        "--max-upload-bytes",
        type=int,
        default=int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024))),
        help="maximum allowed upload body size in bytes",
    )
    parser.add_argument(
        "--upstream-base-url",
        default=os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:8000/v1"),
        help="upstream OpenAI-compatible base url, e.g. http://127.0.0.1:8000/v1",
    )
    parser.add_argument(
        "--upstream-api-key",
        default=os.getenv("UPSTREAM_API_KEY", ""),
        help="fallback upstream api key when the incoming request has no Authorization header",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("PROXY_TIMEOUT", "600")),
        help="upstream timeout in seconds",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    load_dotenv_file(os.path.join(os.getcwd(), ".env"))
    args = parse_args(argv)
    config = ProxyConfig(
        upstream_base_url=args.upstream_base_url,
        upstream_api_key=args.upstream_api_key,
        timeout=args.timeout,
        upload_dir=os.path.abspath(args.upload_dir),
        public_base_url=args.public_base_url,
        max_upload_bytes=args.max_upload_bytes,
    )

    ResponsesProxyHandler.config = config
    server = ThreadingHTTPServer((args.host, args.port), ResponsesProxyHandler)
    server.daemon_threads = True

    log(
        "INFO",
        "responses proxy started",
        host=args.host,
        port=args.port,
        upstream_base_url=config.upstream_base_url,
        upload_dir=config.upload_dir,
        public_base_url=config.public_base_url,
        max_upload_bytes=config.max_upload_bytes,
        pid=os.getpid(),
        thread=threading.current_thread().name,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("INFO", "responses proxy stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
