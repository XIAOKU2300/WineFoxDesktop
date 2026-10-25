"""Public, credential-free LoRA network probes with bounded test downloads."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp
from civitai_api import CivitaiClient, HuggingFaceClient, close_shared_api_sessions
from desktop_runtime.lora_download import _require_public_redirect_url, _safe_error
from desktop_runtime.lora_search import LoraSearchService
from winefox_core.lora_transfer import response


async def main(args):
    proxy = args.proxy
    report_dir = ROOT / "reports/lora-network-20261015"
    report_dir.mkdir(parents=True, exist_ok=True)
    client = CivitaiClient(proxy=proxy, timeout=15, network_max_retries=2, proxy_circuit_breaker=False)
    hf = HuggingFaceClient(proxy=proxy, timeout=15, network_max_retries=2, proxy_circuit_breaker=False,
        download_redirect_validator=lambda url: _require_public_redirect_url(url, proxy=proxy))
    report = {"stage": args.stage, "proxy": proxy, "credentials_used": False}
    try:
        if args.stage == "channels":
            async def check(name, operation):
                started = time.monotonic()
                result = {"name": name}
                try:
                    data = await asyncio.wait_for(operation(), timeout=24)
                    result.update(ok=True, item_count=len(data.get("items", [])) if isinstance(data, dict) else len(data))
                    if name == "tiny-repo":
                        result["files"] = [item.get("rfilename") for item in data.get("siblings", [])]
                except Exception as exc:
                    result.update(ok=False, error_type=type(exc).__name__)
                result["seconds"] = round(time.monotonic() - started, 2)
                print(json.dumps(result, ensure_ascii=True), flush=True)
                return result
            report["results"] = await asyncio.gather(
                check("rest-small", lambda: client._get_json("https://civitai.com/api/v1/models?query=watercolor&limit=3&types=LORA&nsfw=false")),
                check("web-index", lambda: client._search_loras_via_meilisearch("watercolor", limit=3, base_models=["Illustrious"])),
                check("tiny-repo", lambda: hf._get_json("https://huggingface.co/api/models/Jingya/tiny-stable-diffusion-lora-64?blobs=true")),
            )
        elif args.stage == "download":
            report["phase"] = "metadata"
            repo = "Jingya/tiny-stable-diffusion-lora-64"
            metadata = await hf._get_json(f"https://huggingface.co/api/models/{repo}?blobs=true")
            files = [item for item in metadata.get("siblings", []) if str(item.get("rfilename", "")).endswith((".safetensors", ".bin"))]
            files.sort(key=lambda item: (not item["rfilename"].endswith(".safetensors"), item.get("size", 0)))
            if not files:
                raise RuntimeError("No public tiny LoRA weights found")
            selected = files[0]
            name = selected["rfilename"]
            report["file"] = name
            if any(part in {"", ".", ".."} for part in name.split("/")):
                raise RuntimeError("Invalid test repository path")
            url = hf._hf_file_url(repo, name)
            if metadata.get("sha"):
                url = url.replace("/resolve/main/", f"/resolve/{metadata['sha']}/")
            report["phase"] = "probe"
            size = int(selected.get("size") or (selected.get("lfs") or {}).get("size") or 0)
            if size:
                report.update(declared_size=size, size_source="immutable-repository-metadata")
            else:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), cookie_jar=aiohttp.DummyCookieJar()) as session:
                    async with response(hf, session, url, headers={"Range": "bytes=0-0"}) as result:
                        match = re.search(r"/(\d+)$", result.headers.get("Content-Range", ""))
                        size = int(match[1]) if match else int(result.headers.get("Content-Length") or 0)
                        report.update(probe_status=result.status, declared_size=size)
                        if result.status not in {200, 206}:
                            raise RuntimeError("Test download denied: HTTP status")
            if not 0 < size <= 8 * 1024 * 1024:
                raise RuntimeError("Test download denied: 8 MiB size bound")
            target = report_dir / "download" / Path(name).name
            if target.exists():
                raise RuntimeError("Test output already exists; preserve previous evidence")
            started = time.monotonic()
            report["phase"] = "download"
            await hf.download_file(url, target)
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            expected = (selected.get("lfs") or {}).get("sha256") or (selected.get("lfs") or {}).get("oid")
            if expected and actual.lower() != expected.lower():
                raise RuntimeError("Downloaded bytes do not match repository SHA256")
            report.update(ok=True, repo=repo, file=name, size=target.stat().st_size, seconds=round(time.monotonic()-started, 2), sha256=actual, expected_sha256=expected, hash_verified=bool(expected))
        else:
            class Config:
                def snapshot(self): return {"lora_download": {"proxy": proxy, "request_timeout": 60, "split": 2}}
                def get_secret(self, name): return ""
            started = time.monotonic()
            service = LoraSearchService(config=Config(), data_root=report_dir / "live-data")
            result = await service.search("watercolor", family="illustrious", sources=["civitai", "huggingface"], limit=5)
            report.update(seconds=round(time.monotonic()-started, 2), item_count=len(result["items"]),
                items=[{key: item.get(key) for key in ("source", "model_id", "version_id", "base_model", "file_name", "nsfw")} for item in result["items"]], errors=result["errors"])
    except Exception as exc:
        report.update(ok=False, error_type=type(exc).__name__, error=_safe_error(exc))
    finally:
        await close_shared_api_sessions()
    target = report_dir / args.output
    if target.parent.resolve() != report_dir.resolve():
        raise RuntimeError("Report must stay in the review directory")
    with target.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--stage", choices=["channels", "download", "search"], required=True)
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
