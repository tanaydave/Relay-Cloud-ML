import argparse
import asyncio
import glob
import os
import sys
from typing import List
import aiohttp
from aiohttp import web

healthy_replicas: List[str] = []
replica_index = 0
registry_dir = ""


async def health_check_loop():
    global healthy_replicas
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
        while True:
            current_replicas = []
            if os.path.isdir(registry_dir):
                files = glob.glob(os.path.join(registry_dir, "*"))
                for f in files:
                    try:
                        with open(f, "r") as fp:
                            endpoint = fp.read().strip()
                            if endpoint:
                                current_replicas.append(endpoint)
                    except Exception:
                        pass

            verified = []
            for ep in current_replicas:
                url = f"http://{ep}/health"
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            verified.append(ep)
                except Exception:
                    pass

            healthy_replicas = sorted(list(set(verified)))
            await asyncio.sleep(5)


async def router_status(request: web.Request):
    return web.json_response({
        "healthy_replicas": healthy_replicas,
        "count": len(healthy_replicas)
    })


async def proxy_handler(request: web.Request):
    global replica_index
    if not healthy_replicas:
        return web.Response(status=503, text="No healthy vLLM replicas available")

    # Round-robin
    endpoint = healthy_replicas[replica_index % len(healthy_replicas)]
    replica_index += 1

    target_url = f"http://{endpoint}{request.rel_url}"

    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("Content-Length", None)

    body = await request.read()

    timeout = aiohttp.ClientTimeout(total=120.0)
    client = request.app["client_session"]

    try:
        req = client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=body,
            timeout=timeout
        )
        upstream_resp = await req

        response = web.StreamResponse(
            status=upstream_resp.status,
            reason=upstream_resp.reason,
            headers={
                k: v for k, v in upstream_resp.headers.items()
                if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")
            }
        )
        await response.prepare(request)

        async for chunk in upstream_resp.content.iter_any():
            await response.write(chunk)

        await response.write_eof()
        upstream_resp.close()
        return response
    except Exception as e:
        return web.Response(status=502, text=f"Bad Gateway: {str(e)}")


async def on_startup(app: web.Application):
    app["client_session"] = aiohttp.ClientSession()
    app["checker_task"] = asyncio.create_task(health_check_loop())


async def on_cleanup(app: web.Application):
    app["checker_task"].cancel()
    await app["client_session"].close()


def main():
    global registry_dir
    parser = argparse.ArgumentParser(description="Relay HPC Round-Robin Router")
    parser.add_argument("--port", type=int, default=8000, help="Router listen port")
    parser.add_argument("--registry", type=str, default=None, help="Registry directory path")
    args = parser.parse_args()

    netid = os.environ.get("NETID", os.environ.get("USER", "mp7264"))
    registry_dir = args.registry or os.environ.get(
        "RELAY_SCRATCH", f"/scratch/{netid}/relay"
    ) + "/registry"

    print(f"[Router] Starting on port {args.port}, watching registry: {registry_dir}")
    os.makedirs(registry_dir, exist_ok=True)

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/router/status", router_status)
    app.router.add_route("*", "/{path:.*}", proxy_handler)

    web.run_app(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
