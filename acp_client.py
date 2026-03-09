import asyncio
import json
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class AcpClient:
    def __init__(self, acp_path: str):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.acp_path = acp_path
        self.request_id = 0
        self.pending_requests: dict[int, asyncio.Future] = {}
        self.notification_callback: Optional[Callable] = None
        self.error_callback: Optional[Callable] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            "node",
            self.acp_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        error = None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                message = json.loads(line.decode())
                if "id" in message:
                    future = self.pending_requests.pop(message["id"], None)
                    if future:
                        future.set_result(message)
                else:
                    await self._handle_notification(message)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error = e
            logger.error("ACP read loop error: %s", e)
        finally:
            if not self._closed:
                await self._notify_error(error)

    async def _notify_error(self, error: Optional[Exception]):
        """Notify all pending requests and error callback of failure."""
        stderr = ""
        if self.process and self.process.stderr:
            try:
                stderr = (await self.process.stderr.read()).decode()
            except Exception:
                pass

        if stderr:
            logger.error("ACP stderr: %s", stderr)
        if error:
            logger.error("ACP error: %s", error)

        if self.error_callback:
            await self.error_callback(error)

        for future in self.pending_requests.values():
            if not future.done():
                future.set_exception(
                    error or Exception("ACP process terminated unexpectedly")
                )
        self.pending_requests.clear()

    async def _handle_notification(self, message: dict):
        if self.notification_callback:
            await self.notification_callback(message)

    async def send_request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params,
        }
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[self.request_id] = future

        self.process.stdin.write((json.dumps(request) + "\n").encode())
        await self.process.stdin.drain()

        return await future

    async def initialize(self, capabilities: dict = None):
        if capabilities is None:
            capabilities = {"fs": {"readTextFile": True, "writeTextFile": True}}
        return await self.send_request(
            "initialize", {"protocolVersion": 1, "clientCapabilities": capabilities}
        )

    async def new_session(self, cwd: str) -> str:
        result = await self.send_request("session/new", {"cwd": cwd, "mcpServers": []})
        return result["result"]["sessionId"]

    async def prompt(self, session_id: str, text: str) -> dict:
        return await self.send_request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )

    async def request_permission(self, permission_id: str, allow: bool = True):
        option_id = "allow_once" if allow else "deny"
        return await self.send_request(
            "client/requestPermission",
            {
                "permissionId": permission_id,
                "outcome": {"outcome": "selected", "optionId": option_id},
            },
        )

    async def close(self):
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self.process:
            self.process.terminate()
            await self.process.wait()
