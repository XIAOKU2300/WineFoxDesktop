"""Bounded real-ComfyUI A/B check; only targets the isolated review instance."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageChops, ImageStat
from desktop_runtime.workflow_builder import prepare_workflow

BASE = "http://127.0.0.1:18789"
BASE_REPORT = ROOT / "reports/lora-review-20261015"
REPORT = BASE_REPORT / "gpu-ab-1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=20) as response:
        return response.read()


def generate(name, workflow):
    for node in workflow.values():
        if node.get("class_type") == "SaveImage":
            node["inputs"]["filename_prefix"] = f"lora-review/{name}"
    started = time.perf_counter()
    accepted = json.loads(request("/prompt", {"prompt": workflow, "client_id": "winefox-lora-review"}))
    prompt_id = accepted["prompt_id"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        history = json.loads(request(f"/history/{prompt_id}")).get(prompt_id)
        if history:
            if history.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"{name}: ComfyUI execution failed")
            for output in history.get("outputs", {}).values():
                images = output.get("images") or []
                if images:
                    data = request("/view?" + urllib.parse.urlencode(images[0]))
                    image = Image.open(io.BytesIO(data)).convert("RGB")
                    target = REPORT / "images" / f"{name}.png"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as stream:
                        stream.write(data)
                    result = {"name": name, "prompt_id": prompt_id, "pixel_sha256": hashlib.sha256(image.tobytes()).hexdigest(), "seconds": round(time.perf_counter() - started, 2), "image": str(target), "size": image.size}
                    print(json.dumps(result), flush=True)
                    return result, image
        time.sleep(.5)
    raise TimeoutError(f"{name}: timed out after 180 seconds")


def main():
    source = json.loads((ROOT / "workflow/workflow_api.json").read_text(encoding="utf-8-sig"))
    # Keep the A/B focused on LoRA binding: the legacy optional websocket
    # output is unavailable with custom nodes disabled in the review server.
    source.pop("save_image_websocket_node", None)
    REPORT.mkdir(parents=True, exist_ok=True)
    params = {"prompt": "a ceramic teapot on a wooden table, still life, warm studio lighting, plain background", "negative_prompt": "person, text, watermark, blurry", "seed": 777123, "width": 512, "height": 512, "steps": 12, "cfg": 6, "loras": []}
    checkpoint = "Illustrious-XL-v2.0.safetensors"
    selection = {"name": "3d_style.safetensors", "strength_model": .8, "strength_clip": 0}
    info = json.loads(request("/object_info/LoraLoader"))
    if selection["name"] not in info["LoraLoader"]["input"]["required"]["lora_name"][0]:
        raise RuntimeError("Required test LoRA is not available")
    previous_path = BASE_REPORT / "baseline/desktop_runtime/workflow_builder.py"
    spec = importlib.util.spec_from_file_location("desktop_runtime._lora_review_previous", previous_path)
    previous = importlib.util.module_from_spec(spec); spec.loader.exec_module(previous)
    plain, _ = prepare_workflow(source, params, checkpoint=checkpoint)
    old, _ = previous.prepare_workflow(source, {**params, "loras": [selection]}, checkpoint=checkpoint)
    fixed, _ = prepare_workflow(source, {**params, "loras": [selection]}, checkpoint=checkpoint)
    native, _ = prepare_workflow(source, params, checkpoint=checkpoint)
    native["900"] = {"class_type": "LoraLoader", "inputs": {"model": ["30", 0], "clip": ["30", 1], "lora_name": selection["name"], "strength_model": .8, "strength_clip": 0}}
    native["31"]["inputs"]["model"] = ["900", 0]
    native["6"]["inputs"]["clip"] = ["900", 1]
    native["33"]["inputs"]["clip"] = ["900", 1]
    results, images = [], []
    for name, workflow in (("baseline", plain), ("legacy-selected", old), ("fixed-selected", fixed), ("native-selected", native)):
        with (REPORT / f"{name}-workflow.json").open("x", encoding="utf-8") as stream:
            json.dump(workflow, stream, ensure_ascii=False, indent=2)
        result, image = generate(name, workflow); results.append(result); images.append(image)
    report = {"base_url": BASE, "checkpoint": checkpoint, "selection": selection, "params": params, "results": results, "old_equals_baseline": results[0]["pixel_sha256"] == results[1]["pixel_sha256"], "fixed_equals_native": results[2]["pixel_sha256"] == results[3]["pixel_sha256"], "fixed_changes_pixels": results[0]["pixel_sha256"] != results[2]["pixel_sha256"], "baseline_fixed_mean_absolute_pixel_difference": ImageStat.Stat(ImageChops.difference(images[0], images[2])).mean}
    with (REPORT / "real-comfyui.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    assert report["old_equals_baseline"], "Legacy reproduction did not match baseline"
    assert report["fixed_equals_native"], "New binding differs from independent native chain"
    assert report["fixed_changes_pixels"], "LoRA produced no pixel change"
    print("Real ComfyUI LoRA A/B: passed", flush=True)


if __name__ == "__main__":
    main()
