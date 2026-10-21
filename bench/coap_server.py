"""CoAP surrogate: an ``aiocoap`` server in a container, the only off-the-shelf CoAP DUT.

No vendor ships a CoAP device image, so the DUT is the reference Python stack serving
two resources: ``/hello`` (GET) and ``/echo`` (PUT/POST return the payload). The
container is built from a generated context under the cache dir so no second
interpreter's dependencies live in this tree. Bound to 127.0.0.1:5683/udp.

    up | down | status | check
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from bench._common import cache_dir, report, require_tool, run
from litmus.device_api import CoapCode, CoapInterface, DeviceRequest

IMAGE = "litmus-bench/coap"
CONTAINER = "litmus-bench-coap"
HOST = "127.0.0.1"
PORT = int(os.environ.get("LITMUS_BENCH_COAP_PORT", "5683"))

_DOCKERFILE = """\
FROM python:3.12-slim
RUN pip install --no-cache-dir aiocoap==0.4.12
COPY server.py /server.py
EXPOSE 5683/udp
CMD ["python", "-u", "/server.py"]
"""

_SERVER = """\
import asyncio

import aiocoap
import aiocoap.resource as resource


class Hello(resource.Resource):
    async def render_get(self, request):
        return aiocoap.Message(payload=b"hello from the litmus bench")


class Echo(resource.Resource):
    async def render_put(self, request):
        return aiocoap.Message(code=aiocoap.CHANGED, payload=request.payload)

    async def render_post(self, request):
        return aiocoap.Message(code=aiocoap.CHANGED, payload=request.payload)


async def main():
    site = resource.Site()
    site.add_resource(["hello"], Hello())
    site.add_resource(["echo"], Echo())
    await aiocoap.Context.create_server_context(site, bind=("0.0.0.0", 5683))
    print("coap server ready", flush=True)
    await asyncio.get_running_loop().create_future()


asyncio.run(main())
"""


def up() -> int:
    """Build the image from the generated context and start the container."""
    docker = require_tool("docker")
    context = cache_dir() / "coap"
    context.mkdir(exist_ok=True)
    (context / "Dockerfile").write_text(_DOCKERFILE)
    (context / "server.py").write_text(_SERVER)
    run([docker, "build", "-q", "-t", IMAGE, str(context)], capture=True)
    run([docker, "rm", "-f", CONTAINER], check=False, capture=True)
    run(
        [docker, "run", "-d", "--name", CONTAINER, "-p", f"{HOST}:{PORT}:5683/udp", IMAGE],
        capture=True,
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        logs = run([docker, "logs", CONTAINER], capture=True).stdout
        if "coap server ready" in logs:
            print(f"coap server up on coap://{HOST}:{PORT}")
            return 0
        time.sleep(0.5)
    sys.exit("coap server did not report ready")


def down() -> int:
    """Remove the container."""
    run([require_tool("docker"), "rm", "-f", CONTAINER], check=False, capture=True)
    print("coap server removed")
    return 0


def status() -> int:
    """Running or not."""
    out = run([require_tool("docker"), "ps", "-q", "-f", f"name={CONTAINER}"], capture=True).stdout
    print(f"coap server {'up' if out.strip() else 'down'}; coap://{HOST}:{PORT}")
    return 0 if out.strip() else 1


async def check() -> int:
    """GET and PUT through ``CoapInterface``."""
    backend = CoapInterface(base_url=f"coap://{HOST}:{PORT}", request_timeout=5.0)
    await backend.connect()
    failures = 0
    response = await backend.request(DeviceRequest(method="GET", path="/hello"))
    failures += report(
        "GET /hello is 2.05 Content",
        response.status == CoapCode.CONTENT and b"litmus" in response.body,
        f"code={response.status} body={response.body!r}",
    )
    response = await backend.request(DeviceRequest(method="PUT", path="/echo", payload=b"ping"))
    failures += report(
        "PUT /echo returns the payload",
        response.status == CoapCode.CHANGED and response.body == b"ping",
        f"code={response.status} body={response.body!r}",
    )
    response = await backend.request(DeviceRequest(method="GET", path="/missing"))
    failures += report(
        "GET /missing is 4.04",
        response.status == CoapCode.NOT_FOUND,
        f"code={response.status}",
    )
    await backend.disconnect()
    return failures


def main(argv: list[str]) -> int:
    """Dispatch ``up``/``down``/``status``/``check``."""
    actions = {"up": up, "down": down, "status": status, "check": lambda: asyncio.run(check())}
    if len(argv) != 1 or argv[0] not in actions:
        sys.exit(f"usage: python -m bench.coap_server {{{'|'.join(actions)}}}")
    return actions[argv[0]]()


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1:]) else 0)
