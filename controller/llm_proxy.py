"""
LLM API Logging Proxy — Standalone FastAPI app on port 9090.

Intercepts outbound LLM API calls, logs request/response to JSONL,
extracts token usage, and applies scrub rules before writing to disk.

Usage:
    python3 -m uvicorn llm_proxy:create_app --factory --host 0.0.0.0 --port 9090
"""

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from scrub import scrub, scrub_dict

TRAFFIC_LOG = Path(os.environ.get("TRAFFIC_LOG", "/srv/audit/traffic.jsonl"))
CAPTURE_STATE_FILE = Path(os.environ.get("CAPTURE_STATE_FILE", "/srv/audit/capture_enabled"))

PROVIDERS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "gemini": "https://generativelanguage.googleapis.com",
}


def _is_capture_enabled() -> bool:
    """Check if traffic capture is enabled (default: True)."""
    if not CAPTURE_STATE_FILE.exists():
        return True
    try:
        return CAPTURE_STATE_FILE.read_text().strip() == "1"
    except Exception:
        return True


def _set_capture_enabled(enabled: bool):
    """Set capture enabled state."""
    CAPTURE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CAPTURE_STATE_FILE.write_text("1" if enabled else "0")


def create_app() -> FastAPI:
    app = FastAPI(title="ClawFactory LLM Proxy", version="1.0.0")
    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0))

    @app.on_event("shutdown")
    async def shutdown():
        await client.aclose()

    @app.get("/health")
    async def health():
        return {"status": "ok", "capture_enabled": _is_capture_enabled(), "providers": list(PROVIDERS.keys())}

    @app.get("/capture")
    async def get_capture():
        return {"enabled": _is_capture_enabled()}

    @app.post("/capture")
    async def set_capture(request: Request):
        data = await request.json()
        enabled = bool(data.get("enabled", True))
        _set_capture_enabled(enabled)
        return {"enabled": enabled}

    @app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(provider: str, path: str, request: Request):
        if provider not in PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {provider}", "available": list(PROVIDERS.keys())},
                status_code=404,
            )

        upstream = PROVIDERS[provider]
        url = f"{upstream}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"

        # Read request body
        request_body_bytes = await request.body()
        request_body = None
        if request_body_bytes:
            try:
                request_body = json.loads(request_body_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                request_body = request_body_bytes.decode("utf-8", errors="replace")

        # Forward headers (exclude host and hop-by-hop)
        skip_headers = {"host", "transfer-encoding", "connection", "keep-alive"}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in skip_headers}

        # Detect streaming request
        is_streaming = False
        if isinstance(request_body, dict):
            is_streaming = request_body.get("stream", False)

        request_id = str(uuid.uuid4())
        start_time = time.time()

        try:
            if is_streaming:
                return await _handle_streaming(
                    client, request.method, url, headers, request_body_bytes,
                    request_id, start_time, provider, path, request_body,
                )
            else:
                resp = await client.request(
                    method=request.method,
                    url=url,
                    headers=headers,
                    content=request_body_bytes,
                )
                duration_ms = (time.time() - start_time) * 1000
                response_body = None
                try:
                    response_body = resp.json()
                except Exception:
                    response_body = resp.text

                _write_log(
                    request_id=request_id,
                    provider=provider,
                    method=request.method,
                    path=path,
                    request_headers=dict(request.headers),
                    request_body=request_body,
                    response_status=resp.status_code,
                    response_body=response_body,
                    duration_ms=duration_ms,
                    streaming=False,
                )

                # Forward response
                response_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in {"transfer-encoding", "content-encoding", "content-length"}
                }
                return JSONResponse(
                    content=response_body if isinstance(response_body, (dict, list)) else {"raw": response_body},
                    status_code=resp.status_code,
                    headers=response_headers,
                )

        except httpx.ConnectError as e:
            duration_ms = (time.time() - start_time) * 1000
            _write_log(
                request_id=request_id,
                provider=provider,
                method=request.method,
                path=path,
                request_headers=dict(request.headers),
                request_body=request_body,
                response_status=502,
                response_body=None,
                duration_ms=duration_ms,
                streaming=False,
                error=str(e),
            )
            return JSONResponse({"error": f"Cannot connect to {provider}: {e}"}, status_code=502)
        except httpx.TimeoutException as e:
            duration_ms = (time.time() - start_time) * 1000
            _write_log(
                request_id=request_id,
                provider=provider,
                method=request.method,
                path=path,
                request_headers=dict(request.headers),
                request_body=request_body,
                response_status=504,
                response_body=None,
                duration_ms=duration_ms,
                streaming=False,
                error=str(e),
            )
            return JSONResponse({"error": f"Timeout connecting to {provider}: {e}"}, status_code=504)

    async def _handle_streaming(
        client, method, url, headers, body_bytes,
        request_id, start_time, provider, path, request_body,
    ):
        """Handle streaming (SSE) responses — accumulate chunks, log after completion."""
        accumulated_data = []
        response_status = None

        # Make a streaming request to get headers first
        resp = await client.send(
            client.build_request(method, url, headers=headers, content=body_bytes),
            stream=True,
        )
        response_status = resp.status_code

        response_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in {"transfer-encoding", "connection"}
        }

        async def stream_body():
            try:
                async for chunk in resp.aiter_bytes():
                    accumulated_data.append(chunk)
                    yield chunk
            finally:
                await resp.aclose()
                duration_ms = (time.time() - start_time) * 1000
                full_response = b"".join(accumulated_data)
                response_body = _parse_sse_response(full_response, provider)
                _write_log(
                    request_id=request_id,
                    provider=provider,
                    method=method,
                    path=path,
                    request_headers=headers,
                    request_body=request_body,
                    response_status=response_status,
                    response_body=response_body,
                    duration_ms=duration_ms,
                    streaming=True,
                )

        return StreamingResponse(
            stream_body(),
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type", "text/event-stream"),
        )

    return app


def _parse_sse_response(data: bytes, provider: str):
    """Parse SSE stream data to extract final response content."""
    text = data.decode("utf-8", errors="replace")
    # Try to parse as JSON lines (some providers send JSONL)
    events = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            payload = line[6:]
            if payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue

    if events:
        return {"_stream_events": len(events), "_last_event": events[-1] if events else None}

    # Fallback: try to parse as single JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Return truncated raw text
        return text[:2000] if len(text) > 2000 else text


def _extract_tokens(body, provider: str) -> tuple[int, int]:
    """Extract token counts from response body based on provider."""
    if not isinstance(body, dict):
        return 0, 0

    usage = body.get("usage", {})
    if not usage:
        # Check _last_event for streaming
        last = body.get("_last_event", {})
        if isinstance(last, dict):
            usage = last.get("usage", {})

    if not usage or not isinstance(usage, dict):
        return 0, 0

    if provider == "anthropic":
        return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    elif provider == "openai":
        return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    elif provider == "gemini":
        return usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)

    # Generic fallback
    tokens_in = usage.get("input_tokens", usage.get("prompt_tokens", 0))
    tokens_out = usage.get("output_tokens", usage.get("completion_tokens", 0))
    return tokens_in, tokens_out


def _write_log(
    request_id: str,
    provider: str,
    method: str,
    path: str,
    request_headers: dict,
    request_body,
    response_status: int,
    response_body,
    duration_ms: float,
    streaming: bool,
    error: str | None = None,
):
    """Write a scrubbed log entry to the traffic JSONL file."""
    if not _is_capture_enabled():
        return

    tokens_in, tokens_out = _extract_tokens(response_body, provider)

    entry = {
        "id": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "provider": provider,
        "method": method,
        "path": path,
        "request_headers": scrub_dict(request_headers),
        "request_body": scrub_dict(request_body),
        "response_status": response_status,
        "response_body": scrub_dict(response_body),
        "duration_ms": round(duration_ms, 1),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "streaming": streaming,
    }
    if error:
        entry["error"] = error

    try:
        TRAFFIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(TRAFFIC_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # Don't let logging failures break the proxy


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=9090)
