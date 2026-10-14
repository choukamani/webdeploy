"""
WebSocket route — real-time log streaming for individual deployments.

Clients connect to ``/ws/logs/{deployment_id}`` and receive log messages
as they are produced by the pipeline orchestrator.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.dependencies import subscribe_logs, unsubscribe_logs

logger = logging.getLogger("webdeploy.ws")

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/logs/{deployment_id}")
async def stream_logs(websocket: WebSocket, deployment_id: str) -> None:
    """
    Stream deployment logs in real time over a WebSocket connection.

    On connect the client is subscribed to an ``asyncio.Queue``.  Each
    message placed on the queue by :func:`broadcast_log` is forwarded to
    the WebSocket as a text frame.  The connection is torn down cleanly
    on client disconnect or unexpected errors.
    """
    await websocket.accept()
    queue: asyncio.Queue | None = None

    try:
        queue = subscribe_logs(deployment_id)
        logger.info(
            "WebSocket client connected for deployment %s", deployment_id[:8],
        )

        while True:
            # Wait for the next log message from the pipeline
            try:
                message = await asyncio.wait_for(queue.get(), timeout=30.0)
                await websocket.send_text(message)
            except asyncio.TimeoutError:
                # Send a keepalive ping to detect stale connections
                try:
                    await websocket.send_text("")
                except Exception:
                    break

    except WebSocketDisconnect:
        logger.info(
            "WebSocket client disconnected for deployment %s", deployment_id[:8],
        )

    except Exception as exc:
        logger.warning(
            "WebSocket error for deployment %s: %s", deployment_id[:8], exc,
        )

    finally:
        if queue is not None:
            unsubscribe_logs(deployment_id, queue)
        # Attempt a graceful close; ignore errors if already closed
        try:
            await websocket.close()
        except Exception:
            pass
        logger.debug(
            "WebSocket cleanup complete for deployment %s", deployment_id[:8],
        )
