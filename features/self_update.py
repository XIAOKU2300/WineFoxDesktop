from .common import *


class SelfUpdateFeature(PluginFeature):
    def _self_update_token(self) -> str:
        env_name = str(getattr(self, "self_update_token_env", "") or "").strip()
        return (
            str(os.environ.get(env_name, "") or "").strip()
            or str(getattr(self, "self_update_access_token", "") or "").strip()
        )
    def _redact_self_update_text(self, text: str) -> str:
        result = str(text or "")
        token = self._self_update_token()
        if token:
            result = result.replace(token, "***")
        result = re.sub(r"(https?://)([^/@\s]+)@([^/\s]+)", r"\1***@\3", result)
        return result
    def _safe_update_remote_text(self, remote_url: str) -> str:
        return self._redact_self_update_text(remote_url or "未配置")
    def _looks_like_path_value(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return (
            text.startswith(("/", "\\", "~"))
            or bool(re.match(r"^[A-Za-z]:[\\/]", text))
            or ".ssh" in text.replace("\\", "/")
        )
    def _looks_like_github_token(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(re.match(r"^(github_pat_|ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9_]+$", text))
    def _is_github_classic_pat(self, value: str) -> bool:
        return str(value or "").strip().startswith("ghp_")
    def _prefer_github_api_zip_update(self, repo_url: str, branch: str) -> bool:
        token = self._self_update_token()
        if not token or not self._self_update_zip_url(repo_url, branch):
            return False
        return not self._is_github_classic_pat(token)
    def _load_self_update_history(self) -> list:
        try:
            if self.self_update_history_path.exists():
                data = json.loads(self.self_update_history_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [item for item in data if isinstance(item, dict)]
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取更新记录失败: {e}")
        return []
    def _save_self_update_history(self, records: list):
        try:
            records = list(records or [])[: self.self_update_history_limit]
            self.self_update_history_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[ComfyUI] 保存更新记录失败: {e}")
    def _append_self_update_history(self, record: dict):
        record = dict(record or {})
        if not record.get("notes"):
            record["notes"] = self._self_update_notes_for_record(record)
        records = self._load_self_update_history()
        records.insert(0, record)
        self._save_self_update_history(records)
    def _format_self_update_time(self, value=None) -> str:
        try:
            stamp = float(value or time.time())
        except Exception:
            stamp = time.time()
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    def _shorten_self_update_text(self, text: str, limit: int = 96) -> str:
        clean = self._redact_self_update_text(str(text or ""))
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) <= limit:
            return clean
        return clean[: max(limit - 1, 1)].rstrip() + "…"
    def _looks_like_self_update_sha(self, value: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{7,40}", str(value or "").strip(), re.I))
    def _is_self_update_target_version(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return bool(re.fullmatch(r"v?\d+(?:\.\d+){1,5}(?:[-+][0-9A-Za-z][0-9A-Za-z_.-]*)?", text, re.I))
    def _self_update_target_ref_candidates(self, target_version: str) -> list:
        version = str(target_version or "").strip()
        if not self._is_self_update_target_version(version):
            return []
        candidates = [version]
        if version.lower().startswith("v"):
            candidates.append(version[1:])
        else:
            candidates.insert(0, f"v{version}")
        result = []
        seen = set()
        for item in candidates:
            key = item.lower()
            if key and key not in seen:
                seen.add(key)
                result.append(item)
        return result
    def _parse_self_update_version_key(self, value: str):
        text = str(value or "").strip().strip("\"'")
        if text.lower().startswith("v"):
            text = text[1:]
        match = re.fullmatch(
            r"(\d+(?:\.\d+){0,5})(?:-([0-9A-Za-z][0-9A-Za-z_.-]*))?(?:\+[0-9A-Za-z][0-9A-Za-z_.-]*)?",
            text,
            re.I,
        )
        if not match:
            return None
        return tuple(int(part) for part in match.group(1).split(".")), match.group(2) or ""
    def _compare_self_update_versions(self, left: str, right: str):
        left_key = self._parse_self_update_version_key(left)
        right_key = self._parse_self_update_version_key(right)
        if not left_key or not right_key:
            return None
        left_numbers, left_pre = left_key
        right_numbers, right_pre = right_key
        width = max(len(left_numbers), len(right_numbers))
        left_numbers = left_numbers + (0,) * (width - len(left_numbers))
        right_numbers = right_numbers + (0,) * (width - len(right_numbers))
        if left_numbers != right_numbers:
            return 1 if left_numbers > right_numbers else -1
        if left_pre == right_pre:
            return 0
        if not left_pre:
            return 1
        if not right_pre:
            return -1
        left_parts = re.split(r"[.-]", left_pre)
        right_parts = re.split(r"[.-]", right_pre)
        for idx in range(max(len(left_parts), len(right_parts))):
            if idx >= len(left_parts):
                return -1
            if idx >= len(right_parts):
                return 1
            left_part = left_parts[idx]
            right_part = right_parts[idx]
            if left_part == right_part:
                continue
            left_num = left_part.isdigit()
            right_num = right_part.isdigit()
            if left_num and right_num:
                left_int = int(left_part)
                right_int = int(right_part)
                if left_int != right_int:
                    return 1 if left_int > right_int else -1
                continue
            if left_num != right_num:
                return -1 if left_num else 1
            return 1 if left_part > right_part else -1
        return 0
    def _self_update_is_downgrade(self, before_version: str, after_version: str) -> bool:
        comparison = self._compare_self_update_versions(after_version, before_version)
        return comparison is not None and comparison < 0
    def _mark_self_update_downgrade_rejected(self, record: dict, after_version: str, notes: list = None):
        before = str((record or {}).get("before") or PLUGIN_VERSION or "").strip()
        after = str(after_version or "").strip()
        record.update({
            "status": "failed",
            "after": after,
            "message": f"远端版本 {after} 低于当前 {before}，已拒绝降级。需要回退请使用 酒狐更新画图 {after} 或 酒狐强制更新画图。",
            "notes": notes or [],
        })
    def _parse_self_update_target_version(self, raw: str) -> str:
        text = str(raw or "").strip()
        if text.startswith("/"):
            text = text[1:].strip()
        candidates = []
        command_heads = (
            "酒狐强制更新",
            "酒狐画图强制更新",
            "酒狐更新画图强制更新",
            "酒狐更新画图强制",
            "酒狐更新画图",
            "酒狐画图更新",
            "jh强制更新",
            "jh强制更新画图",
            "jh更新画图强制更新",
            "jh更新画图force",
            "jh更新画图",
        )
        lower = text.lower()
        for head in command_heads:
            head_lower = head.lower()
            if lower.startswith(head_lower):
                candidates.append(text[len(head):].strip())
        compact = re.sub(r"\s+", "", text).lower()
        for head in command_heads:
            head_compact = re.sub(r"\s+", "", head).lower()
            if compact.startswith(head_compact):
                candidates.append(compact[len(head_compact):].strip())
        for tail in candidates + [text]:
            tail = str(tail or "").strip()
            if not tail:
                continue
            tail = re.sub(
                r"^(?:强制更新|强制|force[-_ ]?update|force|版本|version|to|=|:|：)+",
                "",
                tail,
                flags=re.I,
            ).strip()
            match = re.search(r"(?<![0-9A-Za-z_])v?\d+(?:\.\d+){1,5}(?:[-+][0-9A-Za-z][0-9A-Za-z_.-]*)?(?![0-9A-Za-z_])", tail, re.I)
            if match:
                version = match.group(0).strip()
                if self._is_self_update_target_version(version):
                    return version
        return ""
    def _clean_self_update_note(self, text: str, limit: int = 88) -> str:
        clean = self._redact_self_update_text(str(text or ""))
        clean = re.sub(r"^\s*[-*•]\s*", "", clean).strip()
        clean = re.sub(r"^[0-9a-f]{7,40}\s+", "", clean, flags=re.I).strip()
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            return ""
        if re.match(r"^(merge|chore|docs?|fix|feat|refactor|update)(\(.+?\))?[:：]\s*", clean, re.I):
            clean = re.sub(r"^[^:：]+[:：]\s*", "", clean).strip()
        return self._shorten_self_update_text(clean, limit)
    def _self_update_notes_from_changelog(self, version: str, source_dir: Path = None, limit: int = 4) -> list:
        version = str(version or "").strip()
        if not version or self._looks_like_self_update_sha(version):
            return []
        changelog_path = Path(source_dir or PLUGIN_DIR) / "CHANGELOG.md"
        try:
            text = changelog_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []
        lines = text.splitlines()
        start = -1
        heading_re = re.compile(rf"^##\s+\[?v?{re.escape(version)}\]?\s*$", re.I)
        for idx, line in enumerate(lines):
            if heading_re.match(line.strip()):
                start = idx + 1
                break
        if start < 0:
            return []
        notes = []
        for line in lines[start:]:
            stripped = line.strip()
            if stripped.startswith("## "):
                break
            if not stripped.startswith(("-", "*", "•")):
                continue
            note = self._clean_self_update_note(stripped, 96)
            if note:
                notes.append(note)
            if len(notes) >= limit:
                break
        return notes
    def _self_update_notes_for_record(self, record: dict, source_dir: Path = None, limit: int = 4) -> list:
        record = record or {}
        existing = [
            self._clean_self_update_note(item, 96)
            for item in (record.get("notes") or [])
            if self._clean_self_update_note(item, 96)
        ]
        if existing:
            return existing[:limit]

        version = str(record.get("after") or record.get("before") or "").strip()
        notes = self._self_update_notes_from_changelog(version, source_dir=source_dir, limit=limit)
        if notes:
            return notes

        commits = [self._clean_self_update_note(item, 96) for item in (record.get("commits") or [])]
        commits = [item for item in commits if item]
        if commits:
            return commits[:limit]

        status = str(record.get("status") or "failed")
        if status == "noop":
            return ["当前已经最新版本"]
        if status == "updated":
            return ["更新完成，重载后生效"]
        return [self._compact_self_update_message(record, cjk=True)]
    def _self_update_display_version(self, value: str, fallback: str = "-") -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        if self._looks_like_self_update_sha(text):
            return fallback
        return self._shorten_self_update_text(text, 18)
    def _compact_self_update_message(self, record: dict, cjk: bool = True) -> str:
        status = str((record or {}).get("status") or "failed")
        message = self._redact_self_update_text(str((record or {}).get("message") or ""))
        low = message.lower()
        if status == "updated":
            return "已更新，稍后自动生效" if cjk else "Updated"
        if status == "noop":
            return "当前已经最新版本" if cjk else "Already up to date"
        if "write access" in low or "403" in low or "http 404" in low or "contents: read" in low:
            return "Token 无仓库读取权限" if cjk else "Token cannot read repo"
        if "gnutls" in low or "tls" in low:
            return "GitHub 连接中断，检查代理" if cjk else "GitHub TLS failed; check proxy"
        if "ssh: not found" in low or "cannot run ssh" in low:
            return "容器缺 SSH 客户端" if cjk else "SSH client missing"
        if "timeout" in low:
            return "更新超时" if cjk else "Update timed out"
        if "dirty" in low or "未提交" in message:
            return "本地代码有未提交改动" if cjk else "Local code has changes"
        if "repo_url" in low:
            return "未配置仓库地址" if cjk else "Repository URL missing"
        if not message:
            return "未知错误" if cjk else "Unknown error"
        first = re.split(r"[；;。]\s*", message, maxsplit=1)[0]
        return self._shorten_self_update_text(first, 58 if cjk else 72)
    def _schedule_self_update_reload(self) -> bool:
        if not getattr(self, "self_update_auto_reload", True):
            return False
        current = getattr(self, "_self_update_reload_task", None)
        if current and not current.done():
            return True
        manager = getattr(self.context, "_star_manager", None)
        reload_func = getattr(manager, "reload", None)
        if not callable(reload_func):
            logger.warning("[ComfyUI] 自更新后自动重载不可用：context._star_manager.reload 不存在")
            return False

        async def _reload_later():
            await asyncio.sleep(1.0)
            try:
                success, message = await reload_func(PLUGIN_ID)
                if success:
                    logger.info("[ComfyUI] 自更新后已自动热重载插件")
                else:
                    logger.warning(f"[ComfyUI] 自更新后自动热重载失败: {message}")
            except Exception as e:
                logger.warning(f"[ComfyUI] 自更新后自动热重载异常: {e}")
                logger.warning(traceback.format_exc())

        self._self_update_reload_task = asyncio.create_task(_reload_later())
        return True
    def _prepare_self_update_env(self) -> tuple:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        askpass_path = None

        proxy = str(getattr(self, "self_update_proxy", "") or "").strip()
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[key] = proxy

        ssh_key = str(getattr(self, "self_update_ssh_key_path", "") or "").strip()
        if ssh_key:
            key_path = Path(ssh_key).expanduser()
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {shlex.quote(str(key_path))} -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new"
            )

        token = self._self_update_token()
        if token:
            username = str(getattr(self, "self_update_username", "x-access-token") or "x-access-token").strip() or "x-access-token"
            if os.name == "nt":
                askpass_path = self.data_dir / "self_update_askpass.bat"
                askpass_path.write_text(
                    "@echo off\r\n"
                    "echo %~1 | findstr /I \"Username\" >nul\r\n"
                    "if %errorlevel%==0 (\r\n"
                    "  echo %JH_GIT_USERNAME%\r\n"
                    ") else (\r\n"
                    "  echo %JH_GIT_TOKEN%\r\n"
                    ")\r\n",
                    encoding="utf-8",
                )
            else:
                askpass_path = self.data_dir / "self_update_askpass.sh"
                askpass_path.write_text(
                    "#!/bin/sh\n"
                    "case \"$1\" in\n"
                    "  *Username*) printf '%s\\n' \"$JH_GIT_USERNAME\" ;;\n"
                    "  *) printf '%s\\n' \"$JH_GIT_TOKEN\" ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                try:
                    askpass_path.chmod(0o700)
                except Exception:
                    pass
            env["GIT_ASKPASS"] = str(askpass_path)
            env["JH_GIT_USERNAME"] = username
            env["JH_GIT_TOKEN"] = token
        return env, askpass_path
    def _cleanup_self_update_env(self, askpass_path):
        if not askpass_path:
            return
        try:
            Path(askpass_path).unlink(missing_ok=True)
        except Exception:
            pass
    async def _run_self_update_git(self, args: list, env: dict = None, timeout: int = None, cwd: Path = None) -> dict:
        cmd = ["git"] + [str(x) for x in (args or [])]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd or PLUGIN_DIR),
                env=env or os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=max(int(timeout or self.self_update_timeout), 30),
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"code": 124, "output": "git command timeout"}
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            return {"code": int(proc.returncode or 0), "output": self._redact_self_update_text(output.strip())}
        except FileNotFoundError:
            return {"code": 127, "output": "系统找不到 git，请先安装 git 或把 git 加进 PATH"}
        except Exception as e:
            return {"code": 1, "output": self._short_user_error(e)}
    async def _self_update_git_text(self, args: list, env: dict = None, default: str = "", cwd: Path = None) -> str:
        ret = await self._run_self_update_git(args, env=env, cwd=cwd)
        if ret.get("code") != 0:
            return default
        return str(ret.get("output") or "").strip()
    async def _read_plugin_version_from_git_ref(self, ref: str, env: dict = None, cwd: Path = None) -> str:
        ref = str(ref or "HEAD").strip() or "HEAD"
        metadata_text = await self._self_update_git_text(["show", f"{ref}:metadata.yaml"], env=env, default="", cwd=cwd)
        version = self._read_plugin_version_from_metadata_text(metadata_text)
        if version:
            return version
        main_text = await self._self_update_git_text(["show", f"{ref}:main.py"], env=env, default="", cwd=cwd)
        return self._read_plugin_version_from_main_text(main_text)
    def _looks_like_remote_with_credentials(self, url: str) -> bool:
        return bool(re.search(r"https?://[^/\s@]+@", str(url or "")))
    def _github_repo_parts_from_url(self, url: str) -> tuple:
        text = str(url or "").strip().rstrip("/")
        match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+)$", text, re.I)
        if not match:
            return "", ""
        owner = match.group(1).strip()
        repo = match.group(2).strip()
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo
    def _self_update_zip_url(self, repo_url: str, branch: str = "") -> str:
        owner, repo = self._github_repo_parts_from_url(repo_url)
        if not owner or not repo:
            return ""
        ref = quote(str(branch or "").strip(), safe="")
        if ref:
            return f"https://api.github.com/repos/{owner}/{repo}/zipball/{ref}"
        return f"https://api.github.com/repos/{owner}/{repo}/zipball"
    def _self_update_auth_error_hint(self, status_code: int = 0) -> str:
        token = self._self_update_token()
        if token:
            return (
                f"GitHub 私库认证失败 HTTP {status_code}：当前 token 没有这个仓库的读取权限。"
                "Fine-grained token 需要选中本仓库并给 Contents: Read；Classic token 需要 repo 权限。"
            )
        return (
            f"GitHub 私库认证失败 HTTP {status_code}：容器里没检测到 HTTPS Token。"
            f"请把 PAT 放进 Docker 环境变量 {self.self_update_token_env}，或填 self_update_settings.access_token。"
        )
    def _read_plugin_version_from_metadata_text(self, text: str) -> str:
        match = re.search(r"(?m)^version:\s*['\"]?([^'\"\s#]+)", str(text or ""))
        if match:
            return match.group(1).strip()
        return ""
    def _read_plugin_version_from_main_text(self, text: str) -> str:
        match = re.search(r"PLUGIN_VERSION\s*=\s*['\"]([^'\"]+)['\"]", str(text or ""))
        if match:
            return match.group(1).strip()
        return ""
    def _read_plugin_version_from_dir(self, plugin_dir: Path) -> str:
        root = Path(plugin_dir)
        try:
            metadata_version = self._read_plugin_version_from_metadata_text(
                (root / "metadata.yaml").read_text(encoding="utf-8", errors="ignore")
            )
            if metadata_version:
                return metadata_version
        except Exception:
            pass
        try:
            return self._read_plugin_version_from_main_text(
                (root / "main.py").read_text(encoding="utf-8", errors="ignore")
            )
        except Exception:
            pass
        return ""
    def _find_plugin_root_in_tree(self, root: Path) -> Path:
        root = Path(root)
        if (root / "main.py").exists() and (root / "metadata.yaml").exists():
            return root
        candidates = [
            path for path in root.rglob("*")
            if path.is_dir() and (path / "main.py").exists() and (path / "metadata.yaml").exists()
        ]
        if not candidates:
            raise RuntimeError("下载的压缩包不像插件仓库，缺少 main.py 或 metadata.yaml")
        return sorted(candidates, key=lambda p: len(p.relative_to(root).parts))[0]
    def _safe_extract_self_update_zip(self, archive_path: Path, extract_dir: Path) -> Path:
        extract_dir = self._assert_self_update_data_child(extract_dir)
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        root_resolved = extract_dir.resolve()
        with zipfile.ZipFile(archive_path) as zf:
            for member in zf.infolist():
                name = member.filename.replace("\\", "/")
                if not name or name.startswith("/") or ".." in Path(name).parts:
                    continue
                target = (extract_dir / name).resolve()
                if not target.is_relative_to(root_resolved):
                    continue
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        return self._find_plugin_root_in_tree(extract_dir)
    async def _download_self_update_zip(self, repo_url: str, branch: str, archive_path: Path) -> tuple:
        zip_url = self._self_update_zip_url(repo_url, branch)
        if not zip_url:
            return "", "zip 兜底仅支持 GitHub 仓库地址"
        headers = {
            "User-Agent": f"{PLUGIN_ID}/{PLUGIN_VERSION}",
            "Accept": "application/vnd.github+json",
        }
        token = self._self_update_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(
            total=max(int(getattr(self, "self_update_timeout", 180) or 180), 30),
            connect=20,
            sock_read=90,
        )
        proxy = str(getattr(self, "self_update_proxy", "") or "").strip() or None
        archive_path = self._assert_self_update_data_child(archive_path)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(zip_url, proxy=proxy, allow_redirects=True) as resp:
                    if resp.status != 200:
                        detail = (await resp.text(errors="replace"))[:500]
                        if resp.status in {401, 403, 404}:
                            return "", f"{self._self_update_auth_error_hint(resp.status)}；GitHub 返回：{self._redact_self_update_text(detail)}"
                        return "", f"GitHub zip 下载失败 HTTP {resp.status}: {self._redact_self_update_text(detail)}"
                    with archive_path.open("wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            if chunk:
                                f.write(chunk)
                    return str(resp.url), ""
        except Exception as e:
            return "", f"GitHub zip 下载失败：{self._short_user_error(e)}"
    async def _ensure_self_update_remote(self, env: dict, mutate: bool = False) -> tuple:
        remote = str(getattr(self, "self_update_remote", "origin") or "origin").strip() or "origin"
        remote_url = await self._self_update_git_text(["remote", "get-url", remote], env=env)
        configured_url = str(getattr(self, "self_update_repo_url", "") or "").strip()
        if not remote_url and configured_url and mutate:
            if self._looks_like_remote_with_credentials(configured_url):
                return "", "repo_url 里不要写 token/密码，请改用 token_env 或 SSH deploy key"
            added = await self._run_self_update_git(["remote", "add", remote, configured_url], env=env)
            if added.get("code") != 0:
                return "", added.get("output") or "添加 git remote 失败"
            remote_url = configured_url
        if not remote_url:
            return "", f"未配置 git remote：{remote}"
        return remote_url, ""
    async def _self_update_state(self, env: dict = None, mutate_remote: bool = False) -> dict:
        env = env or os.environ.copy()
        inside = await self._run_self_update_git(["rev-parse", "--is-inside-work-tree"], env=env)
        if inside.get("code") != 0 or str(inside.get("output") or "").strip().lower() != "true":
            return {"ok": False, "error": "当前插件目录不是 git 仓库，无法自动更新"}
        branch = await self._self_update_git_text(["rev-parse", "--abbrev-ref", "HEAD"], env=env, default="")
        head = await self._self_update_git_text(["rev-parse", "HEAD"], env=env, default="")
        short = await self._self_update_git_text(["rev-parse", "--short", "HEAD"], env=env, default="")
        status = await self._self_update_git_text(["status", "--porcelain", "--untracked-files=no"], env=env, default="")
        remote_url, remote_error = await self._ensure_self_update_remote(env, mutate=mutate_remote)
        return {
            "ok": not bool(remote_error),
            "error": remote_error,
            "branch": branch,
            "head": head,
            "short": short,
            "dirty": bool(status.strip()),
            "dirty_detail": status.strip(),
            "remote": str(getattr(self, "self_update_remote", "origin") or "origin").strip() or "origin",
            "remote_url": remote_url,
        }
    def _assert_self_update_data_child(self, path: Path) -> Path:
        base = self.data_dir.resolve()
        target = Path(path).resolve()
        if target == base or not target.is_relative_to(base):
            raise RuntimeError(f"拒绝操作非自更新数据目录：{target}")
        return target
    def _copy_plugin_source_from_clone(self, clone_dir: Path):
        clone_dir = Path(clone_dir).resolve()
        target_dir = PLUGIN_DIR.resolve()
        if not (clone_dir / "main.py").exists() or not (clone_dir / "metadata.yaml").exists():
            raise RuntimeError("克隆结果不像插件仓库，缺少 main.py 或 metadata.yaml")

        for src in clone_dir.rglob("*"):
            rel = src.relative_to(clone_dir)
            if rel.parts and rel.parts[0] == ".git":
                continue
            if any(part == "__pycache__" for part in rel.parts):
                continue
            dest = target_dir / rel
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    def _backup_current_plugin_source(self) -> str:
        backup_root = self.data_dir / "self_update_backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = backup_root / time.strftime("%Y%m%d-%H%M%S")
        counter = 1
        while backup_dir.exists():
            backup_dir = backup_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{counter}"
            counter += 1
        data_resolved = self.data_dir.resolve()

        def ignore(dir_path, names):
            ignored = set()
            for name in names:
                if name in {".git", "__pycache__"} or name.endswith((".pyc", ".pyo")):
                    ignored.add(name)
                    continue
                try:
                    child = (Path(dir_path) / name).resolve()
                    if data_resolved == child or data_resolved.is_relative_to(child):
                        ignored.add(name)
                except Exception:
                    pass
            return ignored

        shutil.copytree(PLUGIN_DIR, backup_dir, ignore=ignore)
        return str(backup_dir)
    async def _perform_self_update_from_zip(self, record: dict, repo_url: str = "", reason: str = "", force: bool = False, target_ref: str = "") -> dict:
        repo_url = str(repo_url or getattr(self, "self_update_repo_url", "") or "").strip()
        if not repo_url:
            record["message"] = "缺少 repo_url，无法走 zip 兜底更新"
            return record
        if self._looks_like_remote_with_credentials(repo_url):
            record["message"] = "repo_url 里不要写 token/密码，请改用 token_env 或 SSH deploy key"
            return record

        branch = str(target_ref or record.get("branch") or getattr(self, "self_update_branch", "") or "").strip()
        if branch in {"", "默认分支", "HEAD"}:
            branch = str(getattr(self, "self_update_branch", "") or "").strip()
        record["branch"] = branch or "默认分支"
        if target_ref:
            record["target_ref"] = target_ref
        record["remote"] = self._safe_update_remote_text(repo_url)
        if not record.get("before"):
            record["before"] = PLUGIN_VERSION

        archive_path = self._assert_self_update_data_child(self.data_dir / "self_update_repo.zip")
        extract_dir = self._assert_self_update_data_child(self.data_dir / "self_update_zip")
        zip_source, zip_error = await self._download_self_update_zip(repo_url, branch, archive_path)
        if zip_error:
            prefix = f"{reason}；" if reason else ""
            record["message"] = f"{prefix}{zip_error}"
            return record

        try:
            plugin_root = self._safe_extract_self_update_zip(archive_path, extract_dir)
            after_version = self._read_plugin_version_from_dir(plugin_root)
            notes = self._self_update_notes_from_changelog(after_version, source_dir=plugin_root)
            if after_version and self._self_update_is_downgrade(record.get("before"), after_version) and not force:
                self._mark_self_update_downgrade_rejected(record, after_version, notes=notes)
                return record
            if after_version and str(record.get("before") or "") == after_version and not force:
                record.update({
                    "status": "noop",
                    "after": after_version,
                    "message": "当前已经最新版本",
                    "notes": notes,
                })
                return record
            backup_dir = self._backup_current_plugin_source()
            self._copy_plugin_source_from_clone(plugin_root)
            record.update({
                "status": "updated",
                "after": after_version or record.get("after") or "",
                "message": "强制更新完成" if force else "更新完成",
                "backup": backup_dir,
                "notes": notes,
                "zip_source": self._redact_self_update_text(zip_source),
            })
            return record
        except Exception as e:
            prefix = f"{reason}；" if reason else ""
            record["message"] = f"{prefix}zip 兜底安装失败：{self._short_user_error(e)}"
            return record
        finally:
            try:
                archive_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                if extract_dir.exists():
                    shutil.rmtree(extract_dir)
            except Exception:
                pass
    async def _perform_self_update_from_clone(self, record: dict, env: dict, repo_url: str = "", branch: str = "", reason: str = "", force: bool = False, target_version: str = "") -> dict:
        repo_url = str(repo_url or getattr(self, "self_update_repo_url", "") or "").strip()
        if not repo_url:
            record["message"] = "当前不是 git 仓库；请填写 self_update_settings.repo_url 后再试"
            return record
        if self._looks_like_remote_with_credentials(repo_url):
            record["message"] = "repo_url 里不要写 token/密码，请改用 token_env 或 SSH deploy key"
            return record

        target_version = str(target_version or "").strip()
        target_refs = self._self_update_target_ref_candidates(target_version)
        if target_refs:
            last_result = None
            errors = []
            for target_ref in target_refs:
                trial = dict(record)
                trial["target_version"] = target_version
                trial["target_ref"] = target_ref
                trial["branch"] = target_ref
                last_result = await self._perform_self_update_from_clone(
                    trial,
                    env,
                    repo_url=repo_url,
                    branch=target_ref,
                    reason=reason or f"指定版本 {target_version}",
                    force=True,
                    target_version="",
                )
                if str(last_result.get("status") or "") in {"updated", "noop"}:
                    last_result["target_version"] = target_version
                    last_result["target_ref"] = target_ref
                    return last_result
                errors.append(f"{target_ref}: {last_result.get('message') or '-'}")
            if last_result:
                record.update(last_result)
            record["target_version"] = target_version
            record["target_ref_candidates"] = target_refs
            record["message"] = (
                f"指定版本 {target_version} 更新失败；已尝试 {', '.join(target_refs)}；"
                f"最后错误：{(last_result or {}).get('message') or '; '.join(errors)}"
            )
            return record

        branch = str(branch or getattr(self, "self_update_branch", "") or "").strip()
        record["branch"] = branch or "默认分支"
        record["remote"] = self._safe_update_remote_text(repo_url)
        if self._prefer_github_api_zip_update(repo_url, branch):
            zip_record = await self._perform_self_update_from_zip(record, repo_url=repo_url, reason=reason, force=force, target_ref=branch)
            if zip_record.get("status") == "updated" or "私库认证失败" in str(zip_record.get("message") or ""):
                return zip_record

        clone_dir = self._assert_self_update_data_child(self.data_dir / "self_update_clone")
        if clone_dir.exists():
            shutil.rmtree(clone_dir)

        clone_args = ["clone", "--depth", "1"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend([repo_url, str(clone_dir)])
        clone = await self._run_self_update_git(clone_args, env=env, cwd=self.data_dir)
        if clone.get("code") != 0:
            fallback_reason = f"{reason}；git clone 失败：{clone.get('output') or '-'}" if reason else f"git clone 失败：{clone.get('output') or '-'}"
            return await self._perform_self_update_from_zip(record, repo_url=repo_url, reason=fallback_reason, force=force, target_ref=branch)

        after_short = await self._self_update_git_text(["rev-parse", "--short", "HEAD"], env=env, cwd=clone_dir, default="")
        commits_raw = await self._self_update_git_text(
            ["log", "--oneline", "--decorate=no", "-n", "10"],
            env=env,
            cwd=clone_dir,
            default="",
        )
        record["commits"] = [line.strip() for line in commits_raw.splitlines() if line.strip()][:10]
        after_version = self._read_plugin_version_from_dir(clone_dir)
        notes = self._self_update_notes_from_changelog(after_version, source_dir=clone_dir)
        if after_version and self._self_update_is_downgrade(record.get("before"), after_version) and not force:
            try:
                shutil.rmtree(clone_dir)
            except Exception:
                pass
            self._mark_self_update_downgrade_rejected(record, after_version, notes=notes)
            return record
        if after_version and str(record.get("before") or "") == after_version and not force:
            try:
                shutil.rmtree(clone_dir)
            except Exception:
                pass
            record.update({
                "status": "noop",
                "after": after_version,
                "message": "当前已经最新版本",
                "notes": notes,
            })
            return record
        backup_dir = self._backup_current_plugin_source()
        self._copy_plugin_source_from_clone(clone_dir)
        try:
            shutil.rmtree(clone_dir)
        except Exception:
            pass

        record.update({
            "status": "updated",
            "after": after_version or after_short,
            "after_commit": after_short,
            "message": "强制更新完成" if force else "更新完成",
            "backup": backup_dir,
            "notes": notes,
        })
        return record
    async def _perform_self_update(self, event: AstrMessageEvent, force: bool = False, target_version: str = "") -> dict:
        started = time.time()
        user_id = str(event.get_sender_id())
        env, askpass_path = self._prepare_self_update_env()
        target_version = str(target_version or "").strip()
        if target_version:
            force = True
        record = {
            "time": started,
            "user_id": user_id,
            "status": "failed",
            "message": "",
            "before": "",
            "after": "",
            "branch": "",
            "remote": "",
            "commits": [],
            "notes": [],
            "force": bool(force),
            "target_version": target_version,
        }
        try:
            if not self.self_update_enabled:
                record["message"] = "自更新已关闭"
                return record
            state = await self._self_update_state(env, mutate_remote=True)
            record["before"] = PLUGIN_VERSION
            record["before_commit"] = state.get("short") or ""
            record["branch"] = self.self_update_branch or state.get("branch") or ""
            record["remote"] = self._safe_update_remote_text(state.get("remote_url") or "")
            if not state.get("ok"):
                if "不是 git 仓库" in str(state.get("error") or ""):
                    return await self._perform_self_update_from_clone(record, env, force=force, target_version=target_version)
                record["message"] = state.get("error") or "git 状态检查失败"
                return record
            branch = self.self_update_branch or state.get("branch") or ""
            if force:
                if not target_version and (not branch or branch == "HEAD"):
                    record["message"] = "当前是 detached HEAD，请在 self_update_settings.branch 填写要强制更新的分支"
                    return record
                return await self._perform_self_update_from_clone(
                    record,
                    env,
                    repo_url=state.get("remote_url") or self.self_update_repo_url,
                    branch="" if target_version else branch,
                    reason="强制更新",
                    force=True,
                    target_version=target_version,
                )
            if state.get("dirty") and not self.self_update_allow_dirty:
                record["message"] = "插件代码有未提交改动，已拒绝更新。提交/清理后再试，或开启 allow_dirty_update。"
                record["detail"] = state.get("dirty_detail", "")[:800]
                return record

            if not branch or branch == "HEAD":
                record["message"] = "当前是 detached HEAD，请在 self_update_settings.branch 填写要更新的分支"
                return record

            remote = str(getattr(self, "self_update_remote", "origin") or "origin").strip() or "origin"
            fetch = await self._run_self_update_git(["fetch", "--tags", remote, branch], env=env)
            if fetch.get("code") != 0:
                reason = f"git fetch 失败：{fetch.get('output') or '-'}"
                return await self._perform_self_update_from_zip(
                    record,
                    repo_url=state.get("remote_url") or self.self_update_repo_url,
                    reason=reason,
                )

            before_full = state.get("head") or ""
            remote_full = await self._self_update_git_text(["rev-parse", "FETCH_HEAD"], env=env, default="")
            remote_short = await self._self_update_git_text(["rev-parse", "--short", "FETCH_HEAD"], env=env, default="")
            commits_raw = ""
            if before_full and remote_full and before_full != remote_full:
                commits_raw = await self._self_update_git_text(
                    ["log", "--oneline", "--decorate=no", f"{before_full}..FETCH_HEAD"],
                    env=env,
                    default="",
                )
            commits = [line.strip() for line in commits_raw.splitlines() if line.strip()]
            record["commits"] = commits[:12]

            if before_full and remote_full and before_full == remote_full:
                notes = self._self_update_notes_from_changelog(PLUGIN_VERSION)
                record.update({
                    "status": "noop",
                    "after": PLUGIN_VERSION,
                    "after_commit": state.get("short") or "",
                    "message": "当前已经最新版本",
                    "notes": notes,
                })
                return record

            remote_version = await self._read_plugin_version_from_git_ref("FETCH_HEAD", env=env) if remote_full else ""
            if remote_version and self._self_update_is_downgrade(record.get("before"), remote_version):
                self._mark_self_update_downgrade_rejected(record, remote_version)
                return record

            merge = await self._run_self_update_git(["merge", "--ff-only", "FETCH_HEAD"], env=env)
            if merge.get("code") != 0:
                record["message"] = f"git merge --ff-only 失败：{merge.get('output') or '-'}"
                return record

            after_short = await self._self_update_git_text(["rev-parse", "--short", "HEAD"], env=env, default=remote_short)
            after_version = self._read_plugin_version_from_dir(PLUGIN_DIR) or PLUGIN_VERSION
            notes = self._self_update_notes_from_changelog(after_version)
            record.update({
                "status": "updated",
                "after": after_version,
                "after_commit": after_short,
                "message": "更新完成",
                "notes": notes,
            })
            return record
        except Exception as e:
            logger.error(f"[ComfyUI] 自更新异常: {e}")
            logger.error(traceback.format_exc())
            record["message"] = f"自更新异常：{self._short_user_error(e)}"
            return record
        finally:
            record["elapsed"] = round(time.time() - started, 2)
            self._append_self_update_history(record)
            self._cleanup_self_update_env(askpass_path)
    def _format_self_update_result(self, record: dict) -> str:
        status = str(record.get("status") or "failed")
        before = self._self_update_display_version(record.get("before"), PLUGIN_VERSION)
        after = self._self_update_display_version(record.get("after"), before)
        if status == "updated":
            return f"✅ 画图已更新：{before} -> {after}\n更新记录见图 (｡•̀ᴗ-)✧"
        if status == "noop":
            return f"ℹ️ 当前已经最新版本：{after}\n更新记录见图 (｡•̀ᴗ-)✧"
        summary = self._compact_self_update_message(record, cjk=True)
        return f"❌ 画图更新失败：{summary}\n更新记录见图 (｡•́︿•̀｡)"
    async def _build_self_update_status_text(self) -> str:
        env, askpass_path = self._prepare_self_update_env()
        try:
            state = await self._self_update_state(env)
        finally:
            self._cleanup_self_update_env(askpass_path)
        lines = ["🧩 画图插件更新状态", "━━━━━━━━━━━━━━━━━━"]
        lines.append(f"开关：{'开启' if self.self_update_enabled else '关闭'}")
        lines.append(f"目录：{PLUGIN_DIR}")
        if not state.get("ok"):
            lines.append(f"状态：{state.get('error') or '不可用'}")
            if self.self_update_repo_url:
                lines.append("兜底：已配置 repo_url，可用临时 clone 覆盖更新")
        else:
            lines.append(f"分支：{self.self_update_branch or state.get('branch') or '-'}")
            lines.append(f"当前：{state.get('short') or '-'}")
            lines.append(f"仓库：{self._safe_update_remote_text(state.get('remote_url') or '')}")
            lines.append(f"代码改动：{'有未提交改动' if state.get('dirty') else '干净'}")
        token_source = self.self_update_token_env if os.environ.get(self.self_update_token_env or "") else ("配置 access_token" if self.self_update_access_token else "")
        auth = "SSH deploy key" if self.self_update_ssh_key_path else (token_source or "git 已有凭据/公开仓库")
        lines.append(f"认证：{auth}")
        lines.append(f"代理：{self.self_update_proxy or '未使用'}")
        lines.append(f"自动重载：{'开启' if self.self_update_auto_reload else '关闭'}")
        lines.append("执行：酒狐更新画图")
        lines.append("记录：酒狐更新画图 记录")
        return "\n".join(lines)
    def _self_update_font_path(self) -> str:
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/msyhbd.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Bold.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
            "/usr/share/fonts/truetype/source-han-sans/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/truetype/source-han-sans/SourceHanSansSC-Regular.otf",
            "/usr/share/fonts/truetype/arphic/ukai.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        scan_roots = [
            PLUGIN_DIR / "fonts",
            PLUGIN_DIR / "assets" / "fonts",
            self.data_dir / "fonts",
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path("/app/.fonts"),
            Path("/root/.fonts"),
        ]
        preferred_name_parts = (
            "notosanscjk", "noto sans cjk", "notosanssc", "sourcehansans",
            "source han sans", "wqy", "wenquanyi", "droidsansfallback",
            "msyh", "simhei", "sarasa", "pingfang",
        )
        for item in candidates:
            try:
                if Path(item).exists():
                    return item
            except Exception:
                pass
        discovered = []
        for root in scan_roots:
            try:
                if not Path(root).exists():
                    continue
                for path in Path(root).rglob("*"):
                    if path.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                        continue
                    name = path.name.lower().replace("-", "").replace("_", "")
                    score = 0
                    for index, part in enumerate(preferred_name_parts):
                        compact = part.replace(" ", "").replace("-", "").replace("_", "")
                        if compact in name:
                            score = 100 - index
                            break
                    if score:
                        discovered.append((score, str(path)))
            except Exception:
                continue
        if discovered:
            discovered.sort(reverse=True)
            return discovered[0][1]
        return ""
    def _self_update_font_is_cjk(self, font_path: str) -> bool:
        name = Path(font_path or "").name.lower()
        return any(key in name for key in ("msyh", "simhei", "noto", "cjk", "wqy", "ukai", "uming", "sourcehan", "sarasa"))
    def _self_update_text_has_cjk(self, text: str) -> bool:
        return any(
            "\u3400" <= ch <= "\u9fff"
            or "\uf900" <= ch <= "\ufaff"
            or "\u3040" <= ch <= "\u30ff"
            or "\uac00" <= ch <= "\ud7af"
            for ch in str(text or "")
        )
    def _load_self_update_history_template(self) -> str:
        try:
            return self.self_update_history_template_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取更新记录 HTML 模板失败，将使用 Pillow 兜底: {e}")
            return ""
    def _build_self_update_history_view(self, records: list) -> dict:
        items = []
        for record in (records or [])[:8]:
            status = str(record.get("status") or "failed")
            if status not in {"updated", "noop", "failed"}:
                status = "failed"
            label = "完成" if status == "updated" else ("最新" if status == "noop" else "失败")
            before = self._self_update_display_version(record.get("before"), "旧记录")
            after = self._self_update_display_version(record.get("after"), before)
            if status == "noop":
                version_text = after
            elif before == after:
                version_text = after
            else:
                version_text = f"{before} → {after}"
            notes = self._self_update_notes_for_record(record)
            items.append({
                "status": status,
                "label": label,
                "time": self._format_self_update_time(record.get("time")),
                "version": version_text,
                "summary": self._compact_self_update_message(record, cjk=True),
                "note_title": "更新内容" if status != "failed" else "失败原因",
                "notes": notes[:4],
            })
        return {
            "title": "酒狐画图更新记录",
            "subtitle": "每版改了什么，一眼看完 (｡•̀ᴗ-)✧",
            "generated_at": self._format_self_update_time(),
            "items": items,
            "empty_title": "暂无更新记录",
            "empty_hint": "执行 酒狐更新画图 后会在这里生成记录",
        }
    def _self_update_html_escape(self, value) -> str:
        return html_lib.escape(str(value if value is not None else ""), quote=True)
    def _self_update_disable_shiki_injection(self, html: str) -> str:
        marker = "astrbot-t2i-shiki-runtime"
        text = str(html or "")
        if marker in text:
            return text
        insert = f"  <!-- {marker}: disabled for update history -->\n"
        match = re.search(r"</head\s*>", text, flags=re.I)
        if match:
            return f"{text[:match.start()]}{insert}{text[match.start():]}"
        return insert + text
    def _compose_self_update_history_html(self, template: str, view: dict) -> str:
        head_match = re.search(r"<head\b[^>]*>.*?</head\s*>", str(template or ""), flags=re.I | re.S)
        head = head_match.group(0) if head_match else (
            "<head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=1180, initial-scale=1\"></head>"
        )
        parts = [
            "<body>",
            "  <main class=\"shell\">",
            "    <header>",
            "      <div>",
            f"        <h1>{self._self_update_html_escape(view.get('title'))}</h1>",
            f"        <div class=\"sub\">{self._self_update_html_escape(view.get('subtitle'))}</div>",
            "      </div>",
            f"      <div class=\"stamp\">{self._self_update_html_escape(view.get('generated_at'))}</div>",
            "    </header>",
            "    <div class=\"topline\"></div>",
        ]
        items = list(view.get("items") or [])
        if items:
            parts.append("    <section class=\"records\">")
            for item in items:
                status = str(item.get("status") or "failed")
                if status not in {"updated", "noop", "failed"}:
                    status = "failed"
                parts.extend([
                    f"      <article class=\"card {status}\">",
                    "        <div class=\"row\">",
                    f"          <div class=\"time\">{self._self_update_html_escape(item.get('time'))}</div>",
                    f"          <div class=\"chip\">{self._self_update_html_escape(item.get('label'))}</div>",
                    "        </div>",
                    "        <div class=\"meta\">",
                    f"          <div><b>版本</b> {self._self_update_html_escape(item.get('version'))}</div>",
                    f"          <div><b>状态</b> {self._self_update_html_escape(item.get('summary'))}</div>",
                    "        </div>",
                ])
                notes = list(item.get("notes") or [])
                if notes:
                    parts.extend([
                        f"        <div class=\"notes-title\">{self._self_update_html_escape(item.get('note_title'))}</div>",
                        "        <div class=\"notes\">",
                    ])
                    for note in notes:
                        parts.append(f"          <div class=\"note\">{self._self_update_html_escape(note)}</div>")
                    parts.append("        </div>")
                parts.append("      </article>")
            parts.append("    </section>")
        else:
            parts.extend([
                "    <section class=\"empty\">",
                f"      <div class=\"empty-title\">{self._self_update_html_escape(view.get('empty_title'))}</div>",
                f"      <div>{self._self_update_html_escape(view.get('empty_hint'))}</div>",
                "    </section>",
            ])
        parts.extend([
            "    <footer></footer>",
            "  </main>",
            "</body>",
        ])
        return "<!doctype html>\n<html lang=\"zh-CN\">\n" + head + "\n" + "\n".join(parts) + "\n</html>"
    def _render_self_update_history_template(self, template: str, view: dict) -> str:
        try:
            from jinja2 import Environment
        except ImportError:
            rendered = self._compose_self_update_history_html(template, view)
            return self._self_update_disable_shiki_injection(rendered)
        try:
            rendered = Environment(autoescape=True).from_string(str(template or "")).render(**(view or {}))
        except Exception as e:
            logger.warning(f"[ComfyUI] 本地渲染更新记录 HTML 模板失败，改用内置 body 拼接: {e}")
            rendered = self._compose_self_update_history_html(template, view)
        return self._self_update_disable_shiki_injection(rendered)
    def _write_self_update_history_debug_html(self, html: str):
        try:
            path = self.data_dir / "self_update_history_render.html"
            path.write_text(str(html or ""), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[ComfyUI] 写入更新记录调试 HTML 失败: {e}")
    def _self_update_render_env(self) -> dict:
        env = dict(os.environ)
        env.setdefault("DEBIAN_FRONTEND", "noninteractive")
        try:
            env.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.data_dir / "ms-playwright"))
        except Exception:
            pass
        proxy = str(getattr(self, "self_update_proxy", "") or "").strip()
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = proxy
        return env
    def _apply_self_update_render_env(self):
        env = self._self_update_render_env()
        for key in (
            "DEBIAN_FRONTEND",
            "PLAYWRIGHT_BROWSERS_PATH",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            value = str(env.get(key) or "").strip()
            if value:
                os.environ[key] = value
    async def _run_self_update_python_module(self, args: list, timeout: int = 600) -> dict:
        cmd = [sys.executable or "python"] + ["-m"] + [str(x) for x in (args or [])]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(PLUGIN_DIR),
                env=self._self_update_render_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=max(int(timeout or 600), 60))
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"code": 124, "output": "python module command timeout"}
            output = (stdout or b"").decode("utf-8", errors="replace").strip()
            return {"code": proc.returncode, "output": output[-1800:]}
        except Exception as e:
            return {"code": 1, "output": repr(e)}
    async def _ensure_self_update_playwright_package(self) -> tuple:
        try:
            from playwright.async_api import async_playwright  # noqa: F401
            return True, ""
        except Exception as import_error:
            logger.warning(f"[ComfyUI] 本地渲染缺少 playwright，尝试自动安装: {import_error}")

        result = await self._run_self_update_python_module(["pip", "install", "playwright>=1.44.0"], timeout=600)
        if int(result.get("code", 1)) != 0:
            return False, f"playwright 自动安装失败：{self._short_user_error(result.get('output'), 260)}"

        try:
            import importlib
            importlib.invalidate_caches()
            from playwright.async_api import async_playwright  # noqa: F401
            return True, ""
        except Exception as e:
            return False, f"playwright 已安装但导入失败：{self._short_user_error(e, 260)}"
    def _self_update_playwright_needs_browser_install(self, exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "executable doesn't exist" in text
            or "please run the following command" in text
            or "playwright install" in text
            or "browser was not found" in text
        )
    def _self_update_render_browser_name(self) -> str:
        browser = str(getattr(self, "self_update_history_render_browser", "chromium") or "chromium").strip().lower()
        return browser if browser in {"firefox", "chromium", "webkit"} else "chromium"
    async def _install_self_update_playwright_browser(self) -> tuple:
        browser = self._self_update_render_browser_name()
        logger.warning(f"[ComfyUI] 本地渲染缺少 {browser}，尝试自动安装 playwright {browser}")
        result = await self._run_self_update_python_module(["playwright", "install", browser], timeout=900)
        if int(result.get("code", 1)) == 0:
            return True, ""
        return False, f"{browser} 自动安装失败：{self._short_user_error(result.get('output'), 320)}"
    def _self_update_playwright_needs_deps_install(self, exc: Exception) -> bool:
        text = str(exc or "").lower()
        return (
            "host system is missing dependencies" in text
            or "playwright install-deps" in text
            or "error while loading shared libraries" in text
            or "target page, context or browser has been closed" in text
            or "browser logs:" in text
            or ".so:" in text
            or "missing libraries" in text
        )
    async def _install_self_update_playwright_deps(self) -> tuple:
        browser = self._self_update_render_browser_name()
        logger.warning(f"[ComfyUI] 本地 {browser} 启动失败，尝试自动安装 playwright {browser} 系统依赖")
        result = await self._run_self_update_python_module(["playwright", "install-deps", browser], timeout=1200)
        if int(result.get("code", 1)) == 0:
            return True, ""
        return False, f"{browser} 系统依赖自动安装失败：{self._short_user_error(result.get('output'), 500)}"
    def _self_update_render_file_head(self, path: Path) -> str:
        try:
            if not path.exists():
                return ""
            raw = path.read_bytes()[:240]
            if not raw:
                return "；文件为空"
            text = raw.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", text).strip()
            if text and any(ch.isprintable() for ch in text):
                return f"；文件头：{text[:160]}"
            return f"；文件头 hex：{raw[:64].hex()}"
        except Exception:
            return ""
    def _normalize_self_update_history_render(self, path: str) -> tuple:
        try:
            from PIL import Image as PILImage, ImageOps
        except Exception as e:
            return "", f"缺少 Pillow，无法规范化 HTML 渲染图：{e}"

        try:
            src = Path(str(path or ""))
            if not src.exists():
                return "", f"HTML 渲染图不存在：{path}"
            with PILImage.open(src) as img:
                img = ImageOps.exif_transpose(img)
                original = f"{img.format or '-'} {img.mode} {img.width}x{img.height} {src.stat().st_size}B"
                if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
                    rgba = img.convert("RGBA")
                    base = PILImage.new("RGB", rgba.size, (255, 255, 255))
                    base.paste(rgba, mask=rgba.getchannel("A"))
                    img = base
                else:
                    img = img.convert("RGB")

                max_width = 1080
                max_height = 3600
                scale = min(max_width / max(img.width, 1), max_height / max(img.height, 1), 1.0)
                if scale < 1.0:
                    resample = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS", 1)
                    img = img.resize((max(int(img.width * scale), 1), max(int(img.height * scale), 1)), resample)

                out_path = self.data_dir / "self_update_history_html_send.jpg"
                quality = 88
                while True:
                    img.save(out_path, format="JPEG", quality=quality, optimize=True, progressive=False)
                    size = out_path.stat().st_size
                    if size <= 1800 * 1024 or quality <= 70:
                        break
                    quality -= 6
                logger.info(
                    f"[ComfyUI] 自更新 HTML 记录图已规范化: {original} -> JPEG RGB "
                    f"{img.width}x{img.height} {out_path.stat().st_size}B q={quality}"
                )
                return str(out_path), ""
        except Exception as e:
            src = Path(str(path or ""))
            return "", f"HTML 渲染图规范化失败：{self._short_user_error(e)}{self._self_update_render_file_head(src)}"
    async def _render_self_update_history_image_local(self, rendered_template: str) -> tuple:
        self._apply_self_update_render_env()
        ok, error = await self._ensure_self_update_playwright_package()
        if not ok:
            return "", error

        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            return "", f"playwright 导入失败：{self._short_user_error(e, 260)}"

        out_path = self.data_dir / "self_update_history_local_render.jpg"
        last_error = ""
        installed_browser = False
        installed_deps = False
        for attempt in range(4):
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
                    browser = await browser_type.launch(
                        headless=True,
                        args=launch_args,
                    )
                    page = await browser.new_page(
                        viewport={"width": 1180, "height": 1600},
                        device_scale_factor=1,
                    )
                    await page.emulate_media(media="screen")
                    await page.set_content(str(rendered_template or ""), wait_until="load", timeout=45000)
                    try:
                        await page.evaluate("() => document.fonts && document.fonts.ready")
                    except Exception:
                        pass
                    await page.wait_for_timeout(180)
                    await page.screenshot(
                        path=str(out_path),
                        type="jpeg",
                        quality=92,
                        full_page=True,
                        animations="disabled",
                    )
                    await browser.close()
                    browser = None
                normalized_path, normalize_error = self._normalize_self_update_history_render(str(out_path))
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
        return "", f"本地浏览器渲染失败：{last_error}"
    async def _render_self_update_history_image(self, records: list) -> tuple:
        template = self._load_self_update_history_template()
        if template:
            view = self._build_self_update_history_view(records)
            rendered_template = self._render_self_update_history_template(template, view)
            self._write_self_update_history_debug_html(rendered_template)
            path, error = await self._render_self_update_history_image_local(rendered_template)
            if path:
                return path, ""
            if error:
                logger.warning(f"[ComfyUI] 本地 HTML 更新记录渲染失败，将使用 Pillow 兜底: {error}")
        return self._render_self_update_history_image_pillow(records)
    def _render_self_update_history_image_pillow(self, records: list) -> tuple:
        try:
            from PIL import Image as PILImage, ImageDraw, ImageFont, ImageFilter
        except Exception as e:
            return "", f"缺少 Pillow，无法渲染图片：{e}"

        font_path = self._self_update_font_path()
        cjk = self._self_update_font_is_cjk(font_path)
        try:
            title_font = ImageFont.truetype(font_path, 38) if font_path else ImageFont.load_default()
            normal_font = ImageFont.truetype(font_path, 22) if font_path else ImageFont.load_default()
            small_font = ImageFont.truetype(font_path, 17) if font_path else ImageFont.load_default()
            mono_font = ImageFont.truetype(font_path, 18) if font_path else ImageFont.load_default()
        except Exception:
            title_font = normal_font = small_font = mono_font = ImageFont.load_default()

        width = 1180
        margin = 54
        max_text_width = width - margin * 2 - 96
        probe = PILImage.new("RGB", (width, 200), "white")
        probe_draw = ImageDraw.Draw(probe)

        def wrap_text(text: str, font) -> list:
            lines = []
            for raw_line in str(text or "").splitlines() or [""]:
                current = ""
                for ch in raw_line:
                    candidate = current + ch
                    try:
                        length = probe_draw.textlength(candidate, font=font)
                    except Exception:
                        length = len(candidate) * 12
                    if current and length > max_text_width:
                        lines.append(current)
                        current = ch
                    else:
                        current = candidate
                lines.append(current)
            return lines

        def text_width(text: str, font) -> float:
            try:
                return probe_draw.textlength(str(text or ""), font=font)
            except Exception:
                return len(str(text or "")) * 10

        def gradient_background(size):
            w, h = size
            img = PILImage.new("RGB", size, (248, 250, 255))
            px = img.load()
            top = (244, 249, 255)
            bottom = (255, 246, 250)
            for y in range(h):
                t = y / max(h - 1, 1)
                for x in range(w):
                    side = x / max(w - 1, 1)
                    r = int(top[0] * (1 - t) + bottom[0] * t + 4 * side)
                    g = int(top[1] * (1 - t) + bottom[1] * t - 2 * side)
                    b = int(top[2] * (1 - t) + bottom[2] * t - 5 * side)
                    px[x, y] = (max(0, min(r, 255)), max(0, min(g, 255)), max(0, min(b, 255)))
            return img.convert("RGBA")

        def rounded_panel(base, box, radius=22, fill=(255, 255, 255, 176), outline=(255, 255, 255, 205)):
            shadow = PILImage.new("RGBA", base.size, (0, 0, 0, 0))
            sd = ImageDraw.Draw(shadow)
            x0, y0, x1, y1 = box
            sd.rounded_rectangle((x0 + 8, y0 + 10, x1 + 8, y1 + 10), radius=radius, fill=(70, 84, 118, 24))
            shadow = shadow.filter(ImageFilter.GaussianBlur(10))
            base.alpha_composite(shadow)
            layer = PILImage.new("RGBA", base.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            ld.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)
            ld.line((x0 + 18, y0 + 1, x1 - 18, y0 + 1), fill=(255, 255, 255, 165), width=1)
            base.alpha_composite(layer)

        def status_palette(status: str):
            if status == "updated":
                return (43, 148, 119), (218, 248, 239), (28, 119, 94)
            if status == "noop":
                return (96, 111, 137), (232, 237, 246), (79, 92, 113)
            return (198, 73, 86), (255, 232, 236), (170, 56, 70)

        labels = {
            "title": "酒狐画图更新记录" if cjk else "JH Update Log",
            "subtitle": f"生成时间 {self._format_self_update_time()}" if cjk else f"Rendered {self._format_self_update_time()}",
            "empty_a": "暂无更新记录" if cjk else "No update records",
            "empty_b": "执行 酒狐更新画图" if cjk else "Run: JH update",
            "version": "版本" if cjk else "Version",
            "notes": "更新内容" if cjk else "Changes",
            "reason": "失败原因" if cjk else "Reason",
            "updated": "完成" if cjk else "DONE",
            "noop": "最新" if cjk else "LATEST",
            "failed": "失败" if cjk else "FAILED",
        }

        cards = []
        for record in (records or [])[:8]:
            status = str(record.get("status") or "failed")
            status_text = labels["updated"] if status == "updated" else (labels["noop"] if status == "noop" else labels["failed"])
            before = self._self_update_display_version(record.get("before"), "old" if not cjk else "旧记录")
            after = self._self_update_display_version(record.get("after"), before)
            version_text = after if status == "noop" or before == after else f"{before} -> {after}"
            note_title = labels["reason"] if status == "failed" else labels["notes"]
            lines = [
                f"{self._format_self_update_time(record.get('time'))}",
                f"{labels['version']}: {version_text}",
                f"{note_title}:",
            ]
            note_items = self._self_update_notes_for_record(record)[:3]
            if not cjk and any(self._self_update_text_has_cjk(item) for item in note_items):
                note_items = [self._compact_self_update_message(record, cjk=False)]
            for item in note_items:
                lines.append("• " + self._shorten_self_update_text(item, 78))
            wrapped = []
            for line in lines:
                wrapped.extend(wrap_text(line, small_font if line.startswith("• ") else normal_font))
            cards.append({"status": status, "label": status_text, "lines": wrapped[:6]})
        if not cards:
            cards = [{"status": "noop", "label": labels["noop"], "lines": [labels["empty_a"], labels["empty_b"]]}]

        line_height = 30
        card_gap = 18
        header_h = 148
        height = header_h + sum(max(112, 44 + len(card["lines"]) * line_height) + card_gap for card in cards) + 34
        image = gradient_background((width, max(height, 520)))
        draw = ImageDraw.Draw(image)

        # Thin anime-style line accents, kept quiet so the records stay readable.
        for x in range(-180, width + 240, 96):
            draw.line((x, 28, x + 260, 288), fill=(224, 232, 245), width=1)
        for y_line in range(120, image.height, 96):
            draw.line((margin, y_line, width - margin, y_line - 32), fill=(247, 224, 232), width=1)
        draw.line((margin, 96, width - margin, 96), fill=(185, 198, 224), width=1)
        draw.line((margin, 101, width - margin, 101), fill=(255, 255, 255), width=1)
        draw.text((margin, 34), labels["title"], fill=(31, 42, 62), font=title_font)
        draw.text((margin, 82), labels["subtitle"], fill=(105, 117, 139), font=small_font)
        draw.text((width - margin - text_width("JH-COMFY", small_font), 40), "JH-COMFY", fill=(140, 153, 176), font=small_font)

        y = header_h
        for card in cards:
            card_height = max(112, 44 + len(card["lines"]) * line_height)
            color, chip_bg, chip_fg = status_palette(card["status"])
            box = (margin, y, width - margin, y + card_height)
            rounded_panel(image, box)
            draw = ImageDraw.Draw(image)
            draw.line((margin + 24, y + 24, margin + 24, y + card_height - 24), fill=color + (210,), width=3)

            chip_text = card["label"]
            chip_w = int(text_width(chip_text, small_font)) + 34
            chip_box = (width - margin - chip_w - 22, y + 20, width - margin - 22, y + 52)
            draw.rounded_rectangle(chip_box, radius=16, fill=chip_bg + (210,), outline=color + (115,), width=1)
            draw.text((chip_box[0] + 17, chip_box[1] + 6), chip_text, fill=chip_fg, font=small_font)

            draw.line((margin + 50, y + 60, width - margin - 36, y + 60), fill=(193, 202, 220, 80), width=1)
            text_y = y + 18
            for idx, line in enumerate(card["lines"]):
                font = mono_font if idx == 0 else (normal_font if idx < 3 else small_font)
                fill = (31, 42, 62) if idx == 0 else (82, 94, 118)
                draw.text((margin + 50, text_y), line, fill=fill, font=font)
                text_y += line_height
            y += card_height + card_gap

        draw.line((margin, image.height - 28, width - margin, image.height - 28), fill=(255, 255, 255, 130), width=1)

        out_path = self.data_dir / "self_update_history_fallback.jpg"
        image.convert("RGB").save(out_path, format="JPEG", quality=90, optimize=True, progressive=False)
        return str(out_path), ""
    def _parse_self_update_action(self, raw: str) -> str:
        text = str(raw or "").strip()
        compact = re.sub(r"\s+", "", text).lower()
        target_version = self._parse_self_update_target_version(text)
        force_prefixes = (
            "酒狐强制更新",
            "酒狐画图强制更新",
            "酒狐更新画图强制更新",
            "酒狐更新画图强制",
            "jh强制更新",
            "jh强制更新画图",
            "jh更新画图强制更新",
            "jh更新画图force",
        )
        if target_version and any(compact.startswith(re.sub(r"\s+", "", item).lower()) for item in force_prefixes):
            return "force_update"
        if compact in {"酒狐强制更新", "酒狐画图强制更新", "酒狐更新画图强制更新", "酒狐更新画图强制", "jh强制更新", "jh强制更新画图", "jh更新画图强制更新", "jh更新画图force"}:
            return "force_update"
        if compact in {"酒狐更新画图记录", "酒狐更新画图历史", "酒狐画图更新记录", "jh更新画图记录", "jh更新画图历史"}:
            return "history"
        if compact in {"酒狐更新画图状态", "jh更新画图状态"}:
            return "status"
        if compact in {"酒狐更新画图帮助", "jh更新画图帮助"}:
            return "help"
        parts = text.split(None, 1)
        action = parts[1].strip().lower() if len(parts) > 1 else ""
        if action in {"记录", "历史", "更新记录", "log", "logs", "history"}:
            return "history"
        if action in {"状态", "检查", "status", "check"}:
            return "status"
        if action in {"帮助", "help", "配置", "auth"}:
            return "help"
        if action in {"强制", "强制更新", "force", "force-update", "force_update"}:
            return "force_update"
        if target_version and action.startswith(("强制更新", "强制", "force")):
            return "force_update"
        return "update"
    def _build_self_update_help_text(self) -> str:
        return "\n".join([
            "🧩 酒狐更新画图",
            "━━━━━━━━━━━━━━━━━━",
            "酒狐更新画图：从 git 仓库拉取并快进更新",
            "酒狐画图强制更新：备份当前源码后从远端覆盖更新",
            "酒狐强制更新 8.0.0：强制覆盖到指定 tag/版本（会依次尝试 v8.0.0 和 8.0.0）",
            "酒狐更新画图 状态：查看仓库/分支/认证状态",
            "酒狐更新画图 记录：发送更新历史图片",
            "",
            "私库推荐：给仓库配置只读 SSH deploy key，然后把插件目录 remote 设为 git@github.com:owner/repo.git。",
            f"HTTPS 私库：把 PAT 放进环境变量 {self.self_update_token_env}，不要写进 remote URL。",
            "代理：默认复用 civitai_settings.proxy / model_download.proxy，也可在 self_update_settings.proxy 单独填写。",
            "安全策略：只允许干净工作区 + git merge --ff-only，不会 reset，也不碰 AstrBot 配置/data。",
        ])
    async def _handle_self_update_command(self, event: AstrMessageEvent, raw_text: str = ""):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可更新画图插件")
            return
        raw_command_text = raw_text or event.message_str or ""
        action = self._parse_self_update_action(raw_command_text)
        target_version = self._parse_self_update_target_version(raw_command_text)
        await self._trace_test_mode(
            event,
            "self_update_command",
            payload={
                "action": action,
                "raw": raw_command_text,
                "target_version": target_version,
                "user": user_id,
                "version": PLUGIN_VERSION,
            },
        )
        if action == "help":
            yield event.plain_result(self._build_self_update_help_text())
            return
        if action == "status":
            yield event.plain_result(await self._build_self_update_status_text())
            return
        if action == "history":
            records = self._load_self_update_history()
            path, error = await self._render_self_update_history_image(records)
            if path:
                yield event.chain_result([Image.fromFileSystem(path)])
            else:
                lines = ["🧾 更新记录", "━━━━━━━━━━━━━━━━━━"]
                for record in records[:10]:
                    lines.append(f"{self._format_self_update_time(record.get('time'))} {record.get('status')} {record.get('before')} -> {record.get('after') or record.get('before')}: {self._compact_self_update_message(record)}")
                if error:
                    lines.append(f"图片渲染失败：{error}")
                yield event.plain_result("\n".join(lines))
            return

        if self._self_update_lock.locked():
            await self._trace_test_mode(event, "self_update_locked", "another update task is running")
            yield event.plain_result("⏳ 更新任务正在执行，请稍后再试")
            return
        async with self._self_update_lock:
            await self._trace_test_mode(event, "self_update_lock_acquired")
            record = await self._perform_self_update(
                event,
                force=(action == "force_update" or bool(target_version)),
                target_version=target_version,
            )
            await self._trace_test_mode(event, "self_update_performed", payload=record)
            path, _error = await self._render_self_update_history_image(self._load_self_update_history())
            await self._trace_test_mode(event, "self_update_history_rendered", payload={"path": path, "error": _error})
            text = self._format_self_update_result(record)
            yield event.plain_result(text)
            if path:
                yield event.chain_result([Image.fromFileSystem(path)])
            if str(record.get("status") or "") == "updated":
                await self._trace_test_mode(event, "self_update_reload_scheduled")
                self._schedule_self_update_reload()
