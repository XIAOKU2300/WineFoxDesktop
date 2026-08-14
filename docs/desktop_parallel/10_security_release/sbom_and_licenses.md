# SBOM 与许可证（SBOM and Licenses）

维护者：Agent 10（quality-security）｜基线：插件 8.5.9

## 1. 版权/作者不一致（必须记录）

| 来源 | 值 | 说明 |
|------|----|------|
| `LICENSE` | **Copyright (c) 2025 lumingya** | MIT 许可证版权声明 |
| `metadata.yaml` | **author: XIAOKU2300**，repo: `XIAOKU2300/astrbot_plugin_comfyui_pro_UltraPlusMax` | 插件元数据作者 |

**结论**：许可证版权人（lumingya）与 metadata 作者（XIAOKU2300）不一致。
发布时必须：在发行说明与法律页同时呈现两者；若二者为同一自然人/实体的不同署名，
由发布经理书面确认后合并声明。未确认前不得在第三方商店/签名流程中单独使用任一名字。

## 2. 组件清单与 Notice

| 组件 | 位置 | 许可证 | Notice |
|------|------|--------|--------|
| Three.js（r1xx 内联） | `assets/vendor/three/three.module.js`、`OrbitControls.js`、`TransformControls.js` | MIT（见 `assets/vendor/three/LICENSE`） | 保留版权头；桌面打包时必须随附 |
| Python 依赖 | `requirements.txt`：opennsfw2[keras3]、Pillow、playwright | MIT/Apache-2.0/BSD 混合 | 以 pip 报告为准生成 SBOM（`sbom-python.json`） |
| Rust/Tauri（桌面层） | 不在本仓库（Agent 01/09 产物） | TBD（合并后补齐） | 发布前必须补入 SBOM，GATE |
| Node 前端（桌面层） | 不在本仓库（Agent 08 产物） | TBD | 发布前必须补入 SBOM（`npm license` 汇总），GATE |
| 工作流 JSON | `workflow/*.json`（17 个） | 随插件 MIT | 工作流为随插件分发的配置数据，随主 LICENSE |
| 自定义节点 Bridge | `comfyui_custom_nodes/ComfyUI-AstrBot-LoraBridge` | 随插件 MIT | v1.4.2；含 Token 鉴权（见 security_gate G04） |
| 模型权重 | 仓库内**无**任何模型文件 | — | **v1 不捆绑模型**；nsfw 审核模型（nsfw_mobilenet_v2_140_224.zip）运行时按需下载，下载即提示第三方许可与风险 |
| 图片素材 | `assets/jiuhu_watermark_huizhi.png` | 插件自有 | 水印素材，随主 LICENSE |

## 3. v1 捆绑策略

- **不捆绑**：TensorFlow/Keras/模型权重/Playwright 浏览器（`desktop_verify.py` G07 强制，
  `.safetensors/.onnx/.pt/.pb/.tflite` 任一出现即 FAIL）。
- 运行时按需：nsfw 模型（GantMan/nsfw_model, Apache-2.0）、Playwright 浏览器内核（Apache-2.0）。
- 例外申请：任何组件需要捆绑必须由发布经理书面批准并补 Notice。

## 4. 生成与验证

```bash
# Python 依赖 SBOM（CI sbom 作业）
pip install -r requirements.txt --dry-run --report reports/pip-report.json
# 生成 reports/sbom-python.json（SPDX-ish 最小格式）

# 许可证核对
python -c "import importlib.metadata as m; [print(d, '->', (m.metadata(d).get('License') or 'unknown')) for d in m.distributions()]"

# 漏洞扫描（免费，无需密钥）
pip install pip-audit && pip-audit -r requirements.txt --desc on
```

## 5. 发布要求

- [ ] SBOM 覆盖 Python/Rust/Node/Three.js 四类组件后归档。
- [ ] `NOTICE` 文件随安装包分发，列出上表全部第三方组件与许可证文本来源。
- [ ] 许可证扫描无 GPL/AGPL/SSPL 传染性依赖（当前 requirements 无）。
