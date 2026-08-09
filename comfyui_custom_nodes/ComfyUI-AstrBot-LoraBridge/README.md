# ComfyUI AstrBot Resource Bridge

This custom node package registers a small HTTP bridge inside the existing
ComfyUI server. It lets AstrBot ask the ComfyUI machine to fetch metadata and
download files directly into ComfyUI-owned model folders.

It does not add visible workflow nodes.

## Routes

- `GET /astrbot/lora/status`
- `POST /astrbot/lora/fetch`
- `POST /astrbot/lora/download`
- `GET /astrbot/lora/task/{task_id}`

`download` defaults to `models/loras` for backward compatibility. Newer AstrBot
versions can pass `target_type`, such as `loras`, `checkpoints`, `controlnet`,
or `controlnet_aux_ckpts`.

## Config

On first ComfyUI start, the bridge creates `config.json` next to this file.
Copy the generated `token` into AstrBot:

`civitai_settings.comfyui_bridge_token`

If the ComfyUI machine reaches Civitai/Hugging Face/GitHub through Clash, set the
Bridge proxy from the ComfyUI machine's network view:

- Clash on the same ComfyUI machine: `http://127.0.0.1:7890`
- Clash LAN on another machine: `http://<LAN-IP>:7890`

Plain `host:port` values are accepted and normalized to `http://host:port`.

Keep the token private if your ComfyUI address is exposed through FRP or a
reverse proxy.
