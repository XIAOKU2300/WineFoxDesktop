from .common import *


class GenerationStatsFeature(PluginFeature):
    def _generation_stats_avatar_cache_hit(self, user_id: str) -> str:
        user_id = self._normalize_qq_user_id(user_id)
        if not user_id:
            return ""
        try:
            target_dir = Path(getattr(self, "generation_stats_avatar_dir", self.data_dir / "generation_stats_avatars"))
            if not target_dir.exists():
                return ""
            candidates = sorted(
                target_dir.glob(f"avatar_{user_id}.*"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            ttl = 7 * 24 * 3600
            now = time.time()
            for path in candidates:
                if path.exists() and path.stat().st_size > 64 and now - path.stat().st_mtime <= ttl:
                    return str(path)
        except Exception:
            return ""
        return ""
    async def _download_generation_stats_avatar_image(self, event: AstrMessageEvent, user_id: str) -> str:
        user_id = self._normalize_qq_user_id(user_id)
        if not user_id:
            return ""
        cached = self._generation_stats_avatar_cache_hit(user_id)
        if cached:
            return cached

        urls = await self._onebot_avatar_url_candidates(event, user_id)
        for url in (
            f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
            f"https://q.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640",
        ):
            if url and url not in urls:
                urls.append(url)
        if not urls:
            return ""

        target_dir = Path(getattr(self, "generation_stats_avatar_dir", self.data_dir / "generation_stats_avatars"))
        target_dir.mkdir(parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_read=8)
        headers = {"User-Agent": "AstrBot-ComfyUI/6.0 rank-avatar"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for url in urls:
                try:
                    async with session.get(url) as resp:
                        if resp.status >= 400:
                            continue
                        data = await resp.read()
                        content_type = resp.headers.get("Content-Type", "")
                        if not self._looks_like_image_download(content_type, data):
                            continue
                        suffix = self._avatar_image_suffix(content_type, data, url)
                        target = target_dir / f"avatar_{user_id}{suffix}"
                        tmp = target.with_suffix(target.suffix + ".tmp")
                        tmp.write_bytes(data)
                        tmp.replace(target)
                        for old in target_dir.glob(f"avatar_{user_id}.*"):
                            if old != target and not str(old).endswith(".tmp"):
                                try:
                                    old.unlink()
                                except Exception:
                                    pass
                        return str(target)
                except Exception:
                    continue
        return ""
    async def _enrich_generation_stats_rank_view(self, event: AstrMessageEvent, view: dict) -> dict:
        if not isinstance(view, dict):
            return view
        rows = list(view.get("top_rows") or [])
        if not rows:
            return view
        semaphore = asyncio.Semaphore(4)

        async def enrich(row: dict):
            user_id = str((row or {}).get("user_id") or "").strip()
            if not user_id:
                return row
            async with semaphore:
                path = await self._download_generation_stats_avatar_image(event, user_id)
            if path:
                row["avatar_path"] = path
                data_url = self._image_file_to_data_url(path)
                if data_url:
                    row["avatar_data_url"] = data_url
            return row

        try:
            view["top_rows"] = await asyncio.gather(*(enrich(dict(row)) for row in rows))
        except Exception as e:
            logger.debug(f"[ComfyUI] 排行榜头像补全失败: {e}")
        return view
    def _generation_stats_date_key(self, offset_days: int = 0) -> str:
        return (datetime.now() + timedelta(days=int(offset_days or 0))).strftime("%Y-%m-%d")
    def _generation_stats_load(self) -> dict:
        data = self._load_json_file_safe(getattr(self, "generation_stats_path", self.data_dir / "daily_generation_stats.json"), default={})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 1)
        days = data.get("days")
        if not isinstance(days, dict):
            data["days"] = {}
        return data
    def _generation_stats_save(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        days = data.get("days")
        if isinstance(days, dict) and len(days) > 120:
            keep_keys = sorted(days.keys())[-120:]
            data["days"] = {key: days[key] for key in keep_keys if key in days}
        self._save_json_file_safe(getattr(self, "generation_stats_path", self.data_dir / "daily_generation_stats.json"), data)
    async def _record_generation_success(self, event: AstrMessageEvent, count: int = 1, source: str = "comfyui", meta: dict = None) -> None:
        try:
            count = int(count or 1)
        except Exception:
            count = 1
        if count <= 0 or not event:
            return
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            return
        try:
            lock = getattr(self, "_generation_stats_lock", None)
            if lock:
                async with lock:
                    data, day_key, user_total, source_key = self._record_generation_success_unlocked(event, user_id, count, source)
                    self._generation_stats_save(data)
            else:
                data, day_key, user_total, source_key = self._record_generation_success_unlocked(event, user_id, count, source)
                self._generation_stats_save(data)
            logger.info(f"[ComfyUI] 生成计数 +{count}: day={day_key} user={user_id} total={user_total} source={source_key}")
        except Exception as e:
            logger.warning(f"[ComfyUI] 写入生成计数失败: {e}")
    def _record_generation_success_unlocked(self, event: AstrMessageEvent, user_id: str, count: int, source: str = "comfyui") -> tuple:
        data = self._generation_stats_load()
        day_key = self._generation_stats_date_key()
        day = data.setdefault("days", {}).setdefault(day_key, {"total": 0, "users": {}})
        users = day.setdefault("users", {})
        user = users.setdefault(user_id, {"count": 0, "name": user_id, "sources": {}})
        user["count"] = int(user.get("count") or 0) + count
        user["name"] = self._sender_display_name(event) or str(user.get("name") or user_id)
        group_id = self._get_group_id(event) if self._is_group_message(event) else None
        if group_id:
            user["last_group_id"] = str(group_id)
        source_key = str(source or "comfyui").strip() or "comfyui"
        sources = user.setdefault("sources", {})
        sources[source_key] = int(sources.get(source_key) or 0) + count
        user["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        day["total"] = int(day.get("total") or 0) + count
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return data, day_key, int(user.get("count") or 0), source_key
    def _generation_stats_day_from_text(self, text: str) -> tuple:
        raw = str(text or "").strip().lower()
        if not raw or raw in {"today", "day", "今日", "今天"}:
            return self._generation_stats_date_key(), "今日"
        if raw in {"yesterday", "昨天", "昨日"}:
            return self._generation_stats_date_key(-1), "昨日"
        match = re.search(r"\b(20\d{2}-\d{1,2}-\d{1,2})\b", raw)
        if match:
            try:
                parsed = datetime.strptime(match.group(1), "%Y-%m-%d").strftime("%Y-%m-%d")
                return parsed, parsed
            except Exception:
                pass
        if raw in {"all", "全部", "总榜", "总排行"}:
            return "all", "总榜"
        return self._generation_stats_date_key(), "今日"
    def _generation_stats_rows(self, day_key: str) -> tuple:
        data = self._generation_stats_load()
        rows = {}
        total = 0
        if day_key == "all":
            for day in (data.get("days") or {}).values():
                if not isinstance(day, dict):
                    continue
                total += int(day.get("total") or 0)
                for user_id, item in (day.get("users") or {}).items():
                    if not isinstance(item, dict):
                        continue
                    row = rows.setdefault(str(user_id), {"count": 0, "name": str(item.get("name") or user_id)})
                    row["count"] += int(item.get("count") or 0)
                    if item.get("name"):
                        row["name"] = str(item.get("name"))
        else:
            day = (data.get("days") or {}).get(day_key) or {}
            total = int(day.get("total") or 0)
            for user_id, item in (day.get("users") or {}).items():
                if isinstance(item, dict):
                    rows[str(user_id)] = {"count": int(item.get("count") or 0), "name": str(item.get("name") or user_id)}
        sorted_rows = sorted(rows.items(), key=lambda kv: (int(kv[1].get("count") or 0), kv[0]), reverse=True)
        return sorted_rows, total
    def _build_generation_stats_rank_view(self, event: AstrMessageEvent, rest: str = "") -> dict:
        day_key, label = self._generation_stats_day_from_text(rest)
        rows, total = self._generation_stats_rows(day_key)
        current_id = str(event.get_sender_id() or "").strip() if event else ""
        max_count = max([int(item.get("count") or 0) for _user_id, item in rows] or [0])
        top_rows = []
        for index, (user_id, item) in enumerate(rows[:10], 1):
            top_rows.append({
                "rank": index,
                "user_id": str(user_id),
                "name": str(item.get("name") or user_id).strip(),
                "count": int(item.get("count") or 0),
                "is_current": bool(current_id and str(user_id) == current_id),
            })
        current_rank = 0
        current_count = 0
        if current_id:
            for index, (user_id, item) in enumerate(rows, 1):
                if str(user_id) == current_id:
                    current_rank = index
                    current_count = int(item.get("count") or 0)
                    break
        return {
            "day_key": day_key,
            "label": label,
            "total": int(total or 0),
            "total_users": len(rows),
            "top_rows": top_rows,
            "current_id": current_id,
            "current_rank": current_rank,
            "current_count": current_count,
            "max_count": int(max_count or 0),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    def _format_generation_stats_rank(self, event: AstrMessageEvent, rest: str = "", view: dict = None) -> str:
        view = view or self._build_generation_stats_rank_view(event, rest)
        label = str(view.get("label") or "今日")
        lines = [
            f"酒狐画图排行榜 - {label}",
            "━━━━━━━━━━━━━━━━━━",
            f"总出图：{int(view.get('total') or 0)} 张",
        ]
        top_rows = list(view.get("top_rows") or [])
        if not top_rows:
            lines.append(f"{label}还没人画图。")
            return "\n".join(lines)
        for row in top_rows:
            lines.append(f"{row.get('rank')}. {row.get('name')} / {row.get('user_id')}：{int(row.get('count') or 0)} 张")
        current_id = str(view.get("current_id") or "").strip()
        current_rank = int(view.get("current_rank") or 0)
        top_ids = {str(row.get("user_id") or "") for row in top_rows}
        if current_id and current_rank and current_id not in top_ids:
            lines.append(f"你：第 {current_rank} 名，{int(view.get('current_count') or 0)} 张")
        return "\n".join(lines)
    def _generation_stats_rank_css_class(self, row: dict) -> str:
        rank = int((row or {}).get("rank") or 0)
        classes = ["rank-row"]
        if rank == 1:
            classes.append("first")
        elif rank == 2:
            classes.append("second")
        elif rank == 3:
            classes.append("third")
        if (row or {}).get("is_current"):
            classes.append("current")
        return " ".join(classes)
    def _compose_generation_stats_rank_html(self, view: dict) -> str:
        escape = self._self_update_html_escape
        label = str((view or {}).get("label") or "今日")
        day_key = str((view or {}).get("day_key") or "")
        date_text = "全部日期" if day_key == "all" else day_key
        max_count = max(int((view or {}).get("max_count") or 0), 1)
        top_rows = list((view or {}).get("top_rows") or [])
        row_parts = []
        if top_rows:
            for row in top_rows:
                rank = int(row.get("rank") or 0)
                count = int(row.get("count") or 0)
                pct = min(max(count / max_count * 100, 5), 100)
                medal = "冠" if rank == 1 else ("亚" if rank == 2 else ("季" if rank == 3 else f"{rank:02d}"))
                current_badge = "<span class=\"you\">你</span>" if row.get("is_current") else ""
                avatar_url = str(row.get("avatar_data_url") or "").strip()
                fallback_letter = (str(row.get("name") or row.get("user_id") or "?").strip() or "?")[:1]
                avatar_markup = (
                    f"<img src=\"{escape(avatar_url)}\" alt=\"\" loading=\"eager\">"
                    if avatar_url
                    else f"<span>{escape(fallback_letter)}</span>"
                )
                row_parts.append(
                    f"<article class=\"{self._generation_stats_rank_css_class(row)}\">"
                    f"  <div class=\"medal\">{escape(medal)}</div>"
                    f"  <div class=\"avatar\">{avatar_markup}</div>"
                    f"  <div class=\"person\">"
                    f"    <div class=\"name\">{escape(row.get('name'))}{current_badge}</div>"
                    f"    <div class=\"uid\">QQ {escape(row.get('user_id'))}</div>"
                    f"    <div class=\"bar\"><span style=\"width:{pct:.1f}%\"></span></div>"
                    f"  </div>"
                    f"  <div class=\"score\"><b>{count}</b><span>张</span></div>"
                    f"</article>"
                )
        else:
            row_parts.append(
                "<section class=\"empty\">"
                f"  <div class=\"empty-title\">{escape(label)}还没人画图</div>"
                "  <div class=\"empty-sub\">第一张图会点亮这里。</div>"
                "</section>"
            )

        current_note = ""
        current_id = str((view or {}).get("current_id") or "").strip()
        current_rank = int((view or {}).get("current_rank") or 0)
        top_ids = {str(row.get("user_id") or "") for row in top_rows}
        if current_id and current_rank and current_id not in top_ids:
            current_note = (
                "<div class=\"current-note\">"
                f"你的排名：第 {current_rank} 名，{int((view or {}).get('current_count') or 0)} 张"
                "</div>"
            )
        elif current_id and not current_rank:
            current_note = "<div class=\"current-note muted\">当前还没上榜，开始出图就有记录。</div>"

        html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=1180, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      width: 1180px;
      background: #f4f6f8;
      color: #1f2933;
      font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", Arial, sans-serif;
      letter-spacing: 0;
    }}
    .shell {{
      width: 1180px;
      padding: 54px 64px 58px;
      background:
        linear-gradient(90deg, rgba(38, 52, 74, .06) 1px, transparent 1px) 0 0 / 48px 48px,
        linear-gradient(180deg, #f7f9fc 0%, #eef3f6 100%);
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 28px;
      padding-bottom: 24px;
      border-bottom: 2px solid #1f2933;
    }}
    .eyebrow {{
      color: #d84d3f;
      font-size: 18px;
      font-weight: 800;
    }}
    h1 {{
      margin: 6px 0 0;
      font-size: 44px;
      line-height: 1.08;
      font-weight: 900;
    }}
    .stamp {{
      text-align: right;
      color: #5f6d7c;
      font-size: 18px;
      line-height: 1.55;
      white-space: nowrap;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 14px;
      margin: 28px 0 24px;
    }}
    .stat {{
      min-height: 104px;
      padding: 18px 20px;
      background: #ffffff;
      border: 1px solid #d9e1ea;
      border-radius: 8px;
      box-shadow: 0 10px 22px rgba(37, 51, 73, .08);
    }}
    .stat span {{
      display: block;
      color: #68788a;
      font-size: 17px;
      font-weight: 700;
    }}
    .stat b {{
      display: block;
      margin-top: 8px;
      color: #17202b;
      font-size: 34px;
      line-height: 1;
    }}
    .board {{
      display: grid;
      gap: 10px;
    }}
    .rank-row {{
      display: grid;
      grid-template-columns: 74px 62px minmax(0, 1fr) 128px;
      align-items: center;
      gap: 18px;
      min-height: 86px;
      padding: 16px 20px 16px 16px;
      background: #ffffff;
      border: 1px solid #d9e1ea;
      border-left: 7px solid #70849a;
      border-radius: 8px;
      box-shadow: 0 8px 18px rgba(37, 51, 73, .07);
    }}
    .rank-row.first {{ border-left-color: #d29b2d; }}
    .rank-row.second {{ border-left-color: #718093; }}
    .rank-row.third {{ border-left-color: #b56a49; }}
    .rank-row.current {{
      background: #fffdf7;
      border-color: #e1bd62;
    }}
    .medal {{
      width: 54px;
      height: 54px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: #202b38;
      color: #ffffff;
      font-size: 22px;
      font-weight: 900;
    }}
    .first .medal {{ background: #d29b2d; }}
    .second .medal {{ background: #718093; }}
    .third .medal {{ background: #b56a49; }}
    .avatar {{
      width: 58px;
      height: 58px;
      border-radius: 50%;
      overflow: hidden;
      display: grid;
      place-items: center;
      background: #edf2f7;
      border: 2px solid #d8e1ea;
      color: #46576b;
      font-size: 22px;
      font-weight: 900;
      flex: none;
    }}
    .avatar img {{
      width: 100%;
      height: 100%;
      display: block;
      object-fit: cover;
    }}
    .person {{ min-width: 0; }}
    .name {{
      display: flex;
      align-items: center;
      gap: 10px;
      color: #17202b;
      font-size: 25px;
      font-weight: 900;
      line-height: 1.2;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .uid {{
      margin-top: 4px;
      color: #718093;
      font-size: 16px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .bar {{
      height: 7px;
      margin-top: 12px;
      border-radius: 999px;
      overflow: hidden;
      background: #e9eef3;
    }}
    .bar span {{
      display: block;
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(90deg, #2f8f83, #d84d3f);
    }}
    .score {{
      text-align: right;
      color: #17202b;
      white-space: nowrap;
    }}
    .score b {{
      font-size: 40px;
      line-height: 1;
    }}
    .score span {{
      margin-left: 6px;
      color: #68788a;
      font-size: 18px;
      font-weight: 800;
    }}
    .you {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 36px;
      height: 26px;
      padding: 0 10px;
      border-radius: 999px;
      background: #202b38;
      color: #fff;
      font-size: 15px;
      font-weight: 900;
      flex: none;
    }}
    .current-note {{
      margin-top: 18px;
      padding: 15px 18px;
      border-radius: 8px;
      background: #202b38;
      color: #ffffff;
      font-size: 20px;
      font-weight: 800;
    }}
    .current-note.muted {{
      background: #ffffff;
      color: #637181;
      border: 1px solid #d9e1ea;
    }}
    .empty {{
      min-height: 230px;
      display: grid;
      place-items: center;
      text-align: center;
      background: #ffffff;
      border: 1px dashed #aebbc8;
      border-radius: 8px;
    }}
    .empty-title {{
      color: #17202b;
      font-size: 32px;
      font-weight: 900;
    }}
    .empty-sub {{
      margin-top: 12px;
      color: #68788a;
      font-size: 20px;
    }}
    footer {{
      display: flex;
      justify-content: space-between;
      margin-top: 24px;
      padding-top: 18px;
      border-top: 1px solid #cbd5df;
      color: #68788a;
      font-size: 16px;
      font-weight: 700;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <div class="eyebrow">JH COMFYUI RANK</div>
        <h1>酒狐画图排行榜</h1>
      </div>
      <div class="stamp">
        <div>{escape(label)} / {escape(date_text)}</div>
        <div>{escape((view or {}).get("generated_at"))}</div>
      </div>
    </header>
    <section class="stats">
      <div class="stat"><span>总出图</span><b>{int((view or {}).get("total") or 0)}</b></div>
      <div class="stat"><span>上榜人数</span><b>{int((view or {}).get("total_users") or 0)}</b></div>
      <div class="stat"><span>榜首出图</span><b>{int((view or {}).get("max_count") or 0)}</b></div>
    </section>
    <section class="board">
      {"".join(row_parts)}
    </section>
    {current_note}
    <footer>
      <span>酒狐画图统计</span>
      <span>Top 10</span>
    </footer>
  </main>
</body>
</html>"""
        return self._self_update_disable_shiki_injection(html)
    def _write_generation_stats_rank_debug_html(self, html: str):
        try:
            path = self.data_dir / "generation_stats_rank_render.html"
            path.write_text(str(html or ""), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[ComfyUI] 写入排行榜调试 HTML 失败: {e}")
    def _normalize_generation_stats_rank_render(self, path: str) -> tuple:
        try:
            from PIL import Image as PILImage, ImageOps
        except Exception as e:
            return "", f"缺少 Pillow，无法规范化排行榜渲染图：{e}"

        try:
            src = Path(str(path or ""))
            if not src.exists():
                return "", f"排行榜渲染图不存在：{path}"
            with PILImage.open(src) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    base = PILImage.new("RGB", rgba.size, (255, 255, 255))
                    base.paste(rgba, mask=rgba.getchannel("A"))
                    img = base
                else:
                    img = img.convert("RGB")

                max_width = 1080
                max_height = 2600
                scale = min(max_width / max(img.width, 1), max_height / max(img.height, 1), 1.0)
                if scale < 1.0:
                    resample = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS", 1)
                    img = img.resize((max(int(img.width * scale), 1), max(int(img.height * scale), 1)), resample)

                out_path = self.data_dir / "generation_stats_rank_send.jpg"
                quality = 90
                while True:
                    img.save(out_path, format="JPEG", quality=quality, optimize=True, progressive=False)
                    size = out_path.stat().st_size
                    if size <= 1600 * 1024 or quality <= 72:
                        break
                    quality -= 6
                logger.info(f"[ComfyUI] 排行榜渲染图已规范化: {img.width}x{img.height} {out_path.stat().st_size}B q={quality}")
                return str(out_path), ""
        except Exception as e:
            src = Path(str(path or ""))
            return "", f"排行榜渲染图规范化失败：{self._short_user_error(e)}{self._self_update_render_file_head(src)}"
    async def _render_generation_stats_rank_image_local(self, view: dict) -> tuple:
        self._apply_self_update_render_env()
        ok, error = await self._ensure_self_update_playwright_package()
        if not ok:
            return "", error

        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            return "", f"playwright 导入失败：{self._short_user_error(e, 260)}"

        rendered_html = self._compose_generation_stats_rank_html(view)
        self._write_generation_stats_rank_debug_html(rendered_html)
        out_path = self.data_dir / "generation_stats_rank_local_render.jpg"
        last_error = ""
        installed_browser = False
        installed_deps = False
        for _attempt in range(4):
            browser = None
            try:
                async with async_playwright() as p:
                    browser_type_name = self._self_update_render_browser_name()
                    browser_type = getattr(p, browser_type_name)
                    launch_args = []
                    if browser_type_name == "chromium":
                        launch_args = [
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--font-render-hinting=medium",
                        ]
                    browser = await browser_type.launch(headless=True, args=launch_args)
                    page = await browser.new_page(
                        viewport={"width": 1180, "height": 1400},
                        device_scale_factor=1,
                    )
                    await page.emulate_media(media="screen")
                    await page.set_content(rendered_html, wait_until="load", timeout=45000)
                    try:
                        await page.evaluate("() => document.fonts && document.fonts.ready")
                    except Exception:
                        pass
                    await page.wait_for_timeout(120)
                    await page.screenshot(
                        path=str(out_path),
                        type="jpeg",
                        quality=92,
                        full_page=True,
                        animations="disabled",
                    )
                    await browser.close()
                    browser = None
                normalized_path, normalize_error = self._normalize_generation_stats_rank_render(str(out_path))
                if normalized_path:
                    return normalized_path, ""
                return "", normalize_error
            except Exception as e:
                last_error = self._short_user_error(e, 900)
                try:
                    if browser:
                        await browser.close()
                except Exception:
                    pass
                if self._self_update_playwright_needs_browser_install(e) and not installed_browser:
                    installed, install_error = await self._install_self_update_playwright_browser()
                    if installed:
                        installed_browser = True
                        continue
                    last_error = install_error
                if self._self_update_playwright_needs_deps_install(e) and not installed_deps:
                    installed, install_error = await self._install_self_update_playwright_deps()
                    if installed:
                        installed_deps = True
                        continue
                    last_error = install_error
                break
        return "", f"本地浏览器渲染排行榜失败：{last_error}"
    def _render_generation_stats_rank_image_pillow(self, view: dict) -> tuple:
        try:
            from PIL import Image as PILImage, ImageDraw, ImageFont, ImageOps
        except Exception as e:
            return "", f"缺少 Pillow，无法渲染排行榜图片：{e}"

        font_path = self._self_update_font_path()
        try:
            title_font = ImageFont.truetype(font_path, 42) if font_path else ImageFont.load_default()
            stat_font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()
            name_font = ImageFont.truetype(font_path, 25) if font_path else ImageFont.load_default()
            normal_font = ImageFont.truetype(font_path, 20) if font_path else ImageFont.load_default()
            small_font = ImageFont.truetype(font_path, 16) if font_path else ImageFont.load_default()
        except Exception:
            title_font = stat_font = name_font = normal_font = small_font = ImageFont.load_default()

        width = 1180
        margin = 64
        top_rows = list((view or {}).get("top_rows") or [])
        row_height = 82
        row_gap = 10
        header_height = 158
        stats_height = 122
        note_height = 56 if int((view or {}).get("current_rank") or 0) and str((view or {}).get("current_id") or "") not in {str(row.get("user_id") or "") for row in top_rows} else 0
        board_height = max(230, len(top_rows) * row_height + max(len(top_rows) - 1, 0) * row_gap)
        height = header_height + stats_height + board_height + note_height + 92
        image = PILImage.new("RGB", (width, max(height, 620)), (246, 248, 251))
        draw = ImageDraw.Draw(image)

        for x in range(0, width, 48):
            draw.line((x, 0, x, image.height), fill=(233, 238, 244), width=1)
        draw.line((margin, 122, width - margin, 122), fill=(31, 41, 51), width=2)
        draw.text((margin, 50), "酒狐画图排行榜", fill=(23, 32, 43), font=title_font)
        label = str((view or {}).get("label") or "今日")
        day_key = str((view or {}).get("day_key") or "")
        date_text = "全部日期" if day_key == "all" else day_key
        stamp = f"{label} / {date_text}\n{(view or {}).get('generated_at') or ''}"
        stamp_lines = str(stamp).splitlines()
        for idx, line in enumerate(stamp_lines):
            try:
                line_w = draw.textlength(line, font=normal_font)
            except Exception:
                line_w = len(line) * 12
            draw.text((width - margin - line_w, 50 + idx * 30), line, fill=(95, 109, 124), font=normal_font)

        def text_width(text: str, font) -> float:
            try:
                return draw.textlength(str(text or ""), font=font)
            except Exception:
                return len(str(text or "")) * 12

        def fit_text(text: str, font, max_width: int) -> str:
            text = str(text or "")
            if text_width(text, font) <= max_width:
                return text
            while len(text) > 1 and text_width(text + "...", font) > max_width:
                text = text[:-1]
            return text + "..."

        def draw_avatar(row: dict, x: int, y: int, size: int):
            path = str((row or {}).get("avatar_path") or "").strip()
            if path and Path(path).exists():
                try:
                    with PILImage.open(path) as src:
                        src = ImageOps.exif_transpose(src).convert("RGBA")
                        ratio = max(size / max(src.width, 1), size / max(src.height, 1))
                        resized = src.resize((max(size, int(src.width * ratio)), max(size, int(src.height * ratio))))
                        left = max((resized.width - size) // 2, 0)
                        top = max((resized.height - size) // 2, 0)
                        avatar = resized.crop((left, top, left + size, top + size))
                        mask = PILImage.new("L", (size, size), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse((0, 0, size - 1, size - 1), fill=255)
                        image.paste(avatar, (x, y), mask)
                        draw.ellipse((x, y, x + size - 1, y + size - 1), outline=(216, 225, 234), width=2)
                        return
                except Exception:
                    pass
            draw.ellipse((x, y, x + size - 1, y + size - 1), fill=(237, 242, 247), outline=(216, 225, 234), width=2)
            letter = (str((row or {}).get("name") or (row or {}).get("user_id") or "?").strip() or "?")[:1]
            try:
                box = draw.textbbox((0, 0), letter, font=normal_font)
                letter_w = box[2] - box[0]
                letter_h = box[3] - box[1]
            except Exception:
                letter_w, letter_h = text_width(letter, normal_font), 20
            draw.text((x + (size - letter_w) / 2, y + (size - letter_h) / 2 - 2), letter, fill=(70, 87, 107), font=normal_font)

        stat_y = 150
        stat_gap = 14
        stat_w = (width - margin * 2 - stat_gap * 2) // 3
        stats = [
            ("总出图", int((view or {}).get("total") or 0)),
            ("上榜人数", int((view or {}).get("total_users") or 0)),
            ("榜首出图", int((view or {}).get("max_count") or 0)),
        ]
        for idx, (key, value) in enumerate(stats):
            x = margin + idx * (stat_w + stat_gap)
            draw.rounded_rectangle((x, stat_y, x + stat_w, stat_y + 100), radius=8, fill=(255, 255, 255), outline=(217, 225, 234), width=1)
            draw.text((x + 20, stat_y + 17), key, fill=(104, 120, 138), font=small_font)
            draw.text((x + 20, stat_y + 45), str(value), fill=(23, 32, 43), font=stat_font)

        y = stat_y + 126
        if not top_rows:
            draw.rounded_rectangle((margin, y, width - margin, y + 230), radius=8, fill=(255, 255, 255), outline=(174, 187, 200), width=1)
            title = f"{label}还没人画图"
            subtitle = "第一张图会点亮这里。"
            draw.text((margin + 350, y + 78), title, fill=(23, 32, 43), font=stat_font)
            draw.text((margin + 385, y + 124), subtitle, fill=(104, 120, 138), font=normal_font)
        else:
            max_count = max(int((view or {}).get("max_count") or 0), 1)
            for row in top_rows:
                rank = int(row.get("rank") or 0)
                count = int(row.get("count") or 0)
                is_current = bool(row.get("is_current"))
                border = (210, 155, 45) if rank == 1 else ((113, 128, 147) if rank == 2 else ((181, 106, 73) if rank == 3 else (112, 132, 154)))
                fill = (255, 253, 247) if is_current else (255, 255, 255)
                draw.rounded_rectangle((margin, y, width - margin, y + row_height), radius=8, fill=fill, outline=(217, 225, 234), width=1)
                draw.rounded_rectangle((margin, y, margin + 7, y + row_height), radius=3, fill=border)
                medal = "冠" if rank == 1 else ("亚" if rank == 2 else ("季" if rank == 3 else f"{rank:02d}"))
                draw.rounded_rectangle((margin + 20, y + 16, margin + 74, y + 70), radius=8, fill=border)
                draw.text((margin + 31, y + 28), medal, fill=(255, 255, 255), font=normal_font)
                draw_avatar(row, margin + 94, y + 14, 58)
                name_x = margin + 172
                name = fit_text(str(row.get("name") or row.get("user_id") or ""), name_font, 520)
                draw.text((name_x, y + 14), name, fill=(23, 32, 43), font=name_font)
                if is_current:
                    badge_x = name_x + int(text_width(name, name_font)) + 12
                    draw.rounded_rectangle((badge_x, y + 20, badge_x + 42, y + 48), radius=14, fill=(32, 43, 56))
                    draw.text((badge_x + 10, y + 24), "你", fill=(255, 255, 255), font=small_font)
                draw.text((name_x, y + 46), fit_text(f"QQ {row.get('user_id')}", small_font, 500), fill=(113, 128, 147), font=small_font)
                bar_x = margin + 760
                bar_y = y + 52
                bar_w = 150
                draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 7), radius=4, fill=(233, 238, 243))
                draw.rounded_rectangle((bar_x, bar_y, bar_x + max(8, int(bar_w * count / max_count)), bar_y + 7), radius=4, fill=(47, 143, 131))
                score = str(count)
                score_w = text_width(score, stat_font)
                draw.text((width - margin - 66 - score_w, y + 22), score, fill=(23, 32, 43), font=stat_font)
                draw.text((width - margin - 52, y + 32), "张", fill=(104, 120, 138), font=normal_font)
                y += row_height + row_gap

        current_id = str((view or {}).get("current_id") or "").strip()
        current_rank = int((view or {}).get("current_rank") or 0)
        top_ids = {str(row.get("user_id") or "") for row in top_rows}
        if current_id and current_rank and current_id not in top_ids:
            note = f"你的排名：第 {current_rank} 名，{int((view or {}).get('current_count') or 0)} 张"
            y += 8
            draw.rounded_rectangle((margin, y, width - margin, y + 54), radius=8, fill=(32, 43, 56))
            draw.text((margin + 18, y + 14), note, fill=(255, 255, 255), font=normal_font)
            y += 64

        draw.line((margin, image.height - 50, width - margin, image.height - 50), fill=(203, 213, 223), width=1)
        draw.text((margin, image.height - 34), "酒狐画图统计", fill=(104, 120, 138), font=small_font)
        out_path = self.data_dir / "generation_stats_rank_fallback.jpg"
        image.save(out_path, format="JPEG", quality=90, optimize=True, progressive=False)
        return str(out_path), ""
    async def _render_generation_stats_rank_image(self, view: dict) -> tuple:
        path, error = await self._render_generation_stats_rank_image_local(view)
        if path:
            return path, ""
        if error:
            logger.warning(f"[ComfyUI] 本地 HTML 排行榜渲染失败，将使用 Pillow 兜底: {error}")
        fallback_path, fallback_error = self._render_generation_stats_rank_image_pillow(view)
        if fallback_path:
            return fallback_path, ""
        return "", fallback_error or error
    def _is_generation_stats_trigger(self, text: str, event: AstrMessageEvent = None) -> bool:
        raw = self._plain_trigger_text(text)
        raw = re.sub(r"^[!！/]+", "", raw).strip()
        return bool(re.match(r"^(?:酒狐排行|酒狐排行榜|酒狐统计|酒狐计数|jh排行|jh排行榜|jh统计|jh计数)(?:\s|$)", raw, re.I))
    def _generation_stats_command_body(self, text: str) -> str:
        raw = self._plain_trigger_text(text)
        raw = re.sub(r"^[!！/]+", "", raw).strip()
        return re.sub(r"^(?:酒狐排行|酒狐排行榜|酒狐统计|酒狐计数|jh排行|jh排行榜|jh统计|jh计数)\s*", "", raw, flags=re.I).strip()
    async def _handle_generation_stats_command(self, event: AstrMessageEvent, rest: str = ""):
        view = self._build_generation_stats_rank_view(event, rest)
        view = await self._enrich_generation_stats_rank_view(event, view)
        path, error = await self._render_generation_stats_rank_image(view)
        if path:
            yield event.chain_result([Image.fromFileSystem(path)])
            return
        text = self._format_generation_stats_rank(event, rest=rest, view=view)
        if error:
            text += f"\n\n图片渲染失败：{error}"
        yield event.plain_result(text)
