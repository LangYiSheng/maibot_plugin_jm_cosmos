from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

from .core import (
    DownloadQuotaManager,
    JMAuthManager,
    JMBrowser,
    JMConfigManager,
    JMDownloadManager,
    JMPacker,
)
from .utils import MessageFormatter, generate_album_filename

logger = logging.getLogger(__name__)


class JMCosmosPluginSection(PluginConfigBase):
    __ui_label__ = "插件配置"

    config_version: str = Field(default="1.0.0", description="配置版本，请勿手动修改")
    download_dir: str = Field(default="./downloads", description="漫画下载目录")
    image_suffix: str = Field(default=".jpg", description="下载图片格式")
    client_type: str = Field(
        default="api",
        description="JM 客户端类型：api 兼容性更好，html 效率更高",
    )
    use_proxy: bool = Field(default=False, description="是否使用代理访问 JM")
    proxy_url: str = Field(
        default="",
        description="代理地址，如 http://host:port 或 socks5://host:port",
    )
    max_concurrent_photos: int = Field(default=3, description="最大并发章节数")
    max_concurrent_images: int = Field(default=5, description="每章节最大并发图片数")
    pack_format: str = Field(
        default="zip",
        description="下载完成后的打包格式：zip、pdf、none",
    )
    pack_password: str = Field(default="", description="ZIP/PDF 打包密码")
    filename_show_password: bool = Field(
        default=False,
        description="文件名中是否追加密码提示",
    )
    auto_delete_after_send: bool = Field(
        default=True,
        description="发送完成后自动删除本地产物",
    )
    send_cover_preview: bool = Field(default=True, description="下载前发送封面预览")
    cover_recall_enabled: bool = Field(
        default=False,
        description="保留配置项；MaiBot 侧暂不实现自动撤回封面消息",
    )
    auto_recall_enabled: bool = Field(
        default=False,
        description="保留配置项；MaiBot 侧暂不实现自动撤回文件消息",
    )
    auto_recall_delay: int = Field(default=60, description="自动撤回延迟秒数")
    enabled_groups: str = Field(
        default="",
        description="允许使用的群号列表，逗号分隔，留空表示全部启用",
    )
    admin_only: bool = Field(default=False, description="是否仅管理员可用")
    admin_list: str = Field(
        default="",
        description="管理员用户 ID 列表，逗号分隔",
    )
    jm_username: str = Field(default="", description="JM 登录用户名")
    jm_password: str = Field(default="", description="JM 登录密码")
    search_page_size: int = Field(default=5, description="搜索/榜单单页展示数量")
    daily_download_limit: int = Field(default=0, description="每用户每日下载次数限制，0 表示不限")
    debug_mode: bool = Field(default=False, description="是否启用调试日志")


class JMCosmosConfig(PluginConfigBase):
    plugin: JMCosmosPluginSection = Field(default_factory=JMCosmosPluginSection)


class JMCosmosMaiBotPlugin(MaiBotPlugin):
    config_model = JMCosmosConfig

    async def on_load(self) -> None:
        self._init_runtime()
        self.ctx.logger.info("JM-Cosmos II (MaiBot) 已加载")

    async def on_unload(self) -> None:
        self.ctx.logger.info("JM-Cosmos II (MaiBot) 已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict[str, object], version: str
    ) -> None:
        del config_data
        self.ctx.logger.info("收到配置更新: scope=%s, version=%s", scope, version)
        if scope == "self":
            self._init_runtime()

    def _init_runtime(self) -> None:
        self.data_dir = Path(__file__).parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        raw_config = self.get_plugin_config_data()
        plugin_config = dict(raw_config.get("plugin", {}))
        self.config_manager = JMConfigManager(plugin_config, self.data_dir)
        self.download_manager = JMDownloadManager(self.config_manager)
        self.browser = JMBrowser(self.config_manager)
        self.auth_manager = JMAuthManager(self.config_manager)
        self.quota_manager = DownloadQuotaManager(self.data_dir / "quota.db")
        self.debug_mode = self.config_manager.debug_mode

    def _check_permission(self, user_id: str, group_id: str) -> tuple[bool, str]:
        if not self.config_manager.is_admin(user_id):
            return False, MessageFormatter.format_error("permission")

        if group_id and not self.config_manager.is_group_enabled(group_id):
            return False, MessageFormatter.format_error("group_disabled")

        return True, ""

    @staticmethod
    def _split_args(args: str) -> list[str]:
        args = (args or "").strip()
        return args.split() if args else []

    @staticmethod
    def _read_group(kwargs: dict[str, Any], key: str, default: str = "") -> str:
        matched_groups = kwargs.get("matched_groups") or {}
        value = matched_groups.get(key, default)
        return str(value or default).strip()

    @staticmethod
    def _encode_file_base64(path: Path) -> str:
        return base64.b64encode(path.read_bytes()).decode("ascii")

    async def _send_text(self, stream_id: str, text: str) -> None:
        await self.ctx.send.text(text, stream_id)

    async def _send_image_path(self, stream_id: str, image_path: Path) -> None:
        await self.ctx.send.image(self._encode_file_base64(image_path), stream_id)

    async def _send_cover_preview(
        self, stream_id: str, album_id: str, detail: dict[str, Any]
    ) -> None:
        cover_dir = self.config_manager.download_dir / "covers"
        cover_path = await self.browser.get_album_cover(album_id, cover_dir)
        if cover_path and cover_path.exists():
            await self._send_image_path(stream_id, cover_path)
        await self._send_text(stream_id, MessageFormatter.format_album_info(detail))

    async def _call_napcat_file_api(
        self,
        output_path: Path,
        user_id: str,
        group_id: str,
    ) -> tuple[bool, str]:
        if group_id:
            api_name = "adapter.napcat.file.upload_group_file"
            params = {
                "group_id": int(group_id) if str(group_id).isdigit() else group_id,
                "file": str(output_path),
                "name": output_path.name,
            }
            target_type = "group"
        elif user_id:
            api_name = "adapter.napcat.file.upload_private_file"
            params = {
                "user_id": int(user_id) if str(user_id).isdigit() else user_id,
                "file": str(output_path),
                "name": output_path.name,
            }
            target_type = "private"
        else:
            return False, "missing-target"

        try:
            result = await self.ctx.api.call(api_name, params=params)
            self.ctx.logger.info(
                "Napcat 文件 API 调用完成: api=%s target=%s result=%s",
                api_name,
                target_type,
                result,
            )
            return True, api_name
        except Exception as exc:
            self.ctx.logger.warning(
                "Napcat 文件 API 调用失败: api=%s target=%s error=%s",
                api_name,
                target_type,
                exc,
            )
            return False, f"{api_name}: {exc}"

    async def _send_file_result(
        self,
        stream_id: str,
        user_id: str,
        group_id: str,
        result_msg: str,
        pack_result: Any,
        cleanup_paths: list[Path] | None = None,
    ) -> None:
        cleanup_paths = cleanup_paths or []

        if not (
            pack_result.success
            and pack_result.output_path
            and pack_result.format != "none"
        ):
            await self._send_text(stream_id, result_msg)
            return

        output_path = Path(pack_result.output_path)
        await self._send_text(stream_id, result_msg)
        sent, detail = await self._call_napcat_file_api(output_path, user_id, group_id)

        if not sent:
            self.ctx.logger.warning(
                "文件发送已降级为文本提示: %s",
                detail,
            )
            await self._send_text(
                stream_id,
                f"文件已生成：{output_path.name}\n本地路径：{output_path}",
            )

        if sent and self.config_manager.auto_delete_after_send:
            for path in cleanup_paths:
                JMPacker.cleanup(path)

    async def _handle_download_quota(self, user_id: str) -> tuple[bool, str]:
        limit = self.config_manager.daily_download_limit
        if limit <= 0 or str(user_id) in self.config_manager.admin_list:
            return True, ""

        can_download, used, total = self.quota_manager.check_quota(user_id, limit)
        if can_download:
            return True, ""
        return False, f"❌ 今日下载次数已达上限 ({used}/{total})\n请明天再试~"

    def _consume_quota_if_needed(self, user_id: str) -> None:
        limit = self.config_manager.daily_download_limit
        if limit > 0 and str(user_id) not in self.config_manager.admin_list:
            self.quota_manager.consume_quota(user_id)

    @Command("jmhelp", pattern=r"^/jmhelp(?:\s+.*)?$")
    async def help_command(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        del kwargs
        await self._send_text(stream_id, MessageFormatter.format_help())
        return True, "显示帮助", 1

    @Command("jm", pattern=r"^/jm(?:\s+(?P<args>.*))?$")
    async def download_album_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        tokens = self._split_args(self._read_group(kwargs, "args"))
        if not tokens:
            msg = "❌ 请提供本子ID\n用法: /jm <ID>\n示例: /jm 123456"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        album_id = tokens[0].strip()
        if not album_id.isdigit():
            msg = MessageFormatter.format_error("invalid_id")
            await self._send_text(stream_id, msg)
            return False, msg, 1

        quota_ok, quota_msg = await self._handle_download_quota(user_id)
        if not quota_ok:
            await self._send_text(stream_id, quota_msg)
            return False, quota_msg, 1

        try:
            await self._send_text(stream_id, f"⏳ 开始下载本子 {album_id}，请稍候...")

            if self.config_manager.send_cover_preview:
                detail = await self.browser.get_album_detail(album_id)
                if detail:
                    await self._send_cover_preview(stream_id, album_id, detail)

            result = await self.download_manager.download_album(album_id)
            if not result.success:
                msg = MessageFormatter.format_error(
                    "download_failed",
                    result.error_message or "",
                )
                await self._send_text(stream_id, msg)
                return False, msg, 1

            output_name = generate_album_filename(
                album_id=album_id,
                password=self.config_manager.pack_password,
                show_password=self.config_manager.filename_show_password,
            )
            packer = JMPacker(
                pack_format=self.config_manager.pack_format,
                password=self.config_manager.pack_password,
            )
            pack_result = packer.pack(result.save_path, output_name)

            self._consume_quota_if_needed(user_id)
            result_msg = MessageFormatter.format_download_result(result, pack_result)
            cleanup_paths = [result.save_path]
            if pack_result.output_path:
                cleanup_paths.append(Path(pack_result.output_path))
            await self._send_file_result(
                stream_id,
                user_id,
                group_id,
                result_msg,
                pack_result,
                cleanup_paths=cleanup_paths,
            )
            return True, result_msg, 1
        except Exception as exc:
            self.ctx.logger.error("下载本子失败: %s", exc)
            if self.debug_mode:
                self.ctx.logger.exception("下载本子异常")
            msg = MessageFormatter.format_error("download_failed", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jmc", pattern=r"^/jmc(?:\s+(?P<args>.*))?$")
    async def download_photo_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        tokens = self._split_args(self._read_group(kwargs, "args"))
        if len(tokens) < 2:
            msg = "❌ 请提供本子ID和章节序号\n用法: /jmc <本子ID> <章节序号>\n示例: /jmc 123456 3"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        album_id = tokens[0].strip()
        if not album_id.isdigit():
            msg = MessageFormatter.format_error("invalid_id")
            await self._send_text(stream_id, msg)
            return False, msg, 1

        try:
            chapter_idx = int(tokens[1])
            if chapter_idx < 1:
                raise ValueError
        except ValueError:
            msg = "❌ 章节序号必须是大于 0 的数字"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        quota_ok, quota_msg = await self._handle_download_quota(user_id)
        if not quota_ok:
            await self._send_text(stream_id, quota_msg)
            return False, quota_msg, 1

        try:
            await self._send_text(
                stream_id,
                f"⏳ 正在获取本子 {album_id} 的第 {chapter_idx} 章节信息...",
            )
            chapter_info = await self.browser.get_photo_id_by_index(album_id, chapter_idx)
            if chapter_info is None:
                msg = (
                    f"❌ 无法获取章节信息\n可能的原因：\n"
                    f"- 本子 {album_id} 不存在\n"
                    f"- 第 {chapter_idx} 章节不存在"
                )
                await self._send_text(stream_id, msg)
                return False, msg, 1

            photo_id, photo_title, total_chapters = chapter_info
            await self._send_text(
                stream_id,
                f"📖 找到章节: {photo_title}\n📚 章节: {chapter_idx}/{total_chapters}\n⏳ 开始下载...",
            )

            result = await self.download_manager.download_photo(photo_id)
            if not result.success:
                msg = MessageFormatter.format_error(
                    "download_failed",
                    result.error_message or "",
                )
                await self._send_text(stream_id, msg)
                return False, msg, 1

            output_name = generate_album_filename(
                album_id=album_id,
                password=self.config_manager.pack_password,
                chapter_idx=chapter_idx,
                show_password=self.config_manager.filename_show_password,
            )
            packer = JMPacker(
                pack_format=self.config_manager.pack_format,
                password=self.config_manager.pack_password,
            )
            pack_result = packer.pack(result.save_path, output_name)

            self._consume_quota_if_needed(user_id)
            result_msg = MessageFormatter.format_download_result(result, pack_result)
            cleanup_paths = [result.save_path]
            if pack_result.output_path:
                cleanup_paths.append(Path(pack_result.output_path))
            await self._send_file_result(
                stream_id,
                user_id,
                group_id,
                result_msg,
                pack_result,
                cleanup_paths=cleanup_paths,
            )
            return True, result_msg, 1
        except Exception as exc:
            self.ctx.logger.error("下载章节失败: %s", exc)
            msg = MessageFormatter.format_error("download_failed", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jms", pattern=r"^/jms(?:\s+(?P<args>.*))?$")
    async def search_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        args = self._read_group(kwargs, "args")
        tokens = self._split_args(args)
        if not tokens:
            msg = "❌ 请提供搜索关键词\n用法: /jms <关键词> [页码]\n示例: /jms 标签名\n示例: /jms 标签名 2"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        page = 1
        if tokens[-1].isdigit():
            page = max(1, int(tokens[-1]))
            tokens = tokens[:-1]
        keyword = " ".join(tokens).strip()
        if not keyword:
            msg = "❌ 搜索关键词不能为空"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        try:
            await self._send_text(stream_id, f"🔍 正在搜索: {keyword} (第{page}页)...")
            results = await self.browser.search_albums(keyword, page)
            results = results[: self.config_manager.search_page_size]
            result_msg = MessageFormatter.format_search_results(results, keyword, page)
            await self._send_text(stream_id, result_msg)
            return True, result_msg, 1
        except Exception as exc:
            self.ctx.logger.error("搜索失败: %s", exc)
            msg = MessageFormatter.format_error("network", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jmi", pattern=r"^/jmi(?:\s+(?P<args>.*))?$")
    async def info_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        tokens = self._split_args(self._read_group(kwargs, "args"))
        if not tokens:
            msg = "❌ 请提供本子ID\n用法: /jmi <ID>\n示例: /jmi 123456"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        album_id = tokens[0]
        if not album_id.isdigit():
            msg = MessageFormatter.format_error("invalid_id")
            await self._send_text(stream_id, msg)
            return False, msg, 1

        try:
            await self._send_text(stream_id, f"📖 正在获取本子 {album_id} 的详情...")
            detail = await self.browser.get_album_detail(album_id)
            if not detail:
                msg = MessageFormatter.format_error("not_found")
                await self._send_text(stream_id, msg)
                return False, msg, 1

            if self.config_manager.send_cover_preview:
                await self._send_cover_preview(stream_id, album_id, detail)
            else:
                await self._send_text(stream_id, MessageFormatter.format_album_info(detail))
            return True, "已发送详情", 1
        except Exception as exc:
            self.ctx.logger.error("获取详情失败: %s", exc)
            msg = MessageFormatter.format_error("network", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jmrank", pattern=r"^/jmrank(?:\s+(?P<args>.*))?$")
    async def ranking_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        tokens = self._split_args(self._read_group(kwargs, "args"))
        ranking_type = tokens[0].lower() if tokens else "week"
        page = 1
        if len(tokens) > 1 and tokens[1].isdigit():
            page = max(1, int(tokens[1]))

        ranking_map = {
            "day": ("日榜", self.browser.get_day_ranking),
            "week": ("周榜", self.browser.get_week_ranking),
            "month": ("月榜", self.browser.get_month_ranking),
        }
        if ranking_type not in ranking_map:
            msg = "❌ 排行榜类型仅支持 day / week / month"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        type_name, loader = ranking_map[ranking_type]
        try:
            await self._send_text(stream_id, f"🏆 正在获取{type_name}第{page}页...")
            results = await loader(page)
            results = results[: self.config_manager.search_page_size]
            result_msg = MessageFormatter.format_ranking_results(
                results,
                ranking_type,
                page,
            )
            await self._send_text(stream_id, result_msg)
            return True, result_msg, 1
        except Exception as exc:
            self.ctx.logger.error("获取排行失败: %s", exc)
            msg = MessageFormatter.format_error("network", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jmrec", pattern=r"^/jmrec(?:\s+(?P<args>.*))?$")
    async def recommend_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        args = self._split_args(self._read_group(kwargs, "args"))
        if args and args[0].lower() == "help":
            msg = MessageFormatter.format_recommend_help()
            await self._send_text(stream_id, msg)
            return True, msg, 1

        categories = JMBrowser.get_category_list()
        orders = JMBrowser.get_order_list()
        times = JMBrowser.get_time_list()

        category = "all"
        order_by = "hot"
        time_range = "week"
        page = 1
        category_set = order_set = time_set = False

        for arg in args:
            arg_lower = arg.lower().strip()
            if arg_lower.isdigit():
                page = max(1, int(arg_lower))
                continue
            if arg_lower in categories:
                if category_set:
                    msg = f"❌ 检测到重复的分类参数: {arg}\n当前已设置分类为: {category}"
                    await self._send_text(stream_id, msg)
                    return False, msg, 1
                category = arg_lower
                category_set = True
                continue
            if arg_lower in orders:
                if order_set:
                    msg = f"❌ 检测到重复的排序参数: {arg}\n当前已设置排序为: {order_by}"
                    await self._send_text(stream_id, msg)
                    return False, msg, 1
                order_by = arg_lower
                order_set = True
                continue
            if arg_lower in times:
                if time_set:
                    msg = f"❌ 检测到重复的时间参数: {arg}\n当前已设置时间为: {time_range}"
                    await self._send_text(stream_id, msg)
                    return False, msg, 1
                time_range = arg_lower
                time_set = True
                continue

            msg = f"❌ 未知参数: {arg}\n💡 使用 /jmrec help 查看帮助"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        try:
            cat_name = MessageFormatter.CATEGORY_NAMES.get(category, category)
            order_name = MessageFormatter.ORDER_NAMES.get(order_by, order_by)
            time_name = MessageFormatter.TIME_NAMES.get(time_range, time_range)
            await self._send_text(
                stream_id,
                f"🎆 正在获取 {cat_name} / {time_name}{order_name} 第{page}页...",
            )
            results = await self.browser.get_category_albums(
                category,
                order_by,
                time_range,
                page,
            )
            results = results[: self.config_manager.search_page_size]
            result_msg = MessageFormatter.format_recommend_results(
                results,
                category,
                order_by,
                time_range,
                page,
            )
            await self._send_text(stream_id, result_msg)
            return True, result_msg, 1
        except Exception as exc:
            self.ctx.logger.error("获取推荐失败: %s", exc)
            msg = MessageFormatter.format_error("network", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jmlogin", pattern=r"^/jmlogin(?:\s+(?P<args>.*))?$")
    async def login_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        tokens = self._split_args(self._read_group(kwargs, "args"))
        if len(tokens) < 2:
            msg = "❌ 请提供用户名和密码\n用法: /jmlogin <用户名> <密码>\n示例: /jmlogin myuser mypass"
            await self._send_text(stream_id, msg)
            return False, msg, 1

        username, password = tokens[0], tokens[1]
        try:
            await self._send_text(stream_id, "🔐 正在登录...")
            success, message = await self.auth_manager.login(username, password)
            reply = f"✅ {message}" if success else f"❌ {message}"
            await self._send_text(stream_id, reply)
            return success, reply, 1
        except Exception as exc:
            self.ctx.logger.error("登录失败: %s", exc)
            msg = MessageFormatter.format_error("network", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1

    @Command("jmlogout", pattern=r"^/jmlogout(?:\s+.*)?$")
    async def logout_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        del kwargs
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        success, message = self.auth_manager.logout()
        reply = f"✅ {message}" if success else f"❌ {message}"
        await self._send_text(stream_id, reply)
        return success, reply, 1

    @Command("jmstatus", pattern=r"^/jmstatus(?:\s+.*)?$")
    async def status_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        del kwargs
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        status = self.auth_manager.get_login_status()
        if status["logged_in"]:
            reply = f"✅ 已登录\n👤 用户名: {status['username']}"
            await self._send_text(stream_id, reply)
            return True, reply, 1

        reply = "❌ 当前未登录\n💡 使用 /jmlogin <用户名> <密码> 登录"
        await self._send_text(stream_id, reply)
        return False, reply, 1

    @Command("jmfav", pattern=r"^/jmfav(?:\s+(?P<args>.*))?$")
    async def favorites_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        group_id: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        has_perm, error_msg = self._check_permission(user_id, group_id)
        if not has_perm:
            await self._send_text(stream_id, error_msg)
            return False, error_msg, 1

        tokens = self._split_args(self._read_group(kwargs, "args"))
        page = 1
        folder_id = "0"
        if tokens:
            if tokens[0].isdigit():
                page = max(1, int(tokens[0]))
            if len(tokens) > 1:
                folder_id = tokens[1]

        logged_in, login_msg = await self.auth_manager.ensure_logged_in()
        if not logged_in:
            reply = f"❌ {login_msg}\n💡 请先使用 /jmlogin 登录"
            await self._send_text(stream_id, reply)
            return False, reply, 1

        try:
            await self._send_text(stream_id, f"⭐ 正在获取收藏夹第{page}页...")
            client = self.auth_manager.get_client()
            albums, folders = await self.browser.get_favorites(client, page, folder_id)
            result_msg = MessageFormatter.format_favorites(albums, folders, page)
            await self._send_text(stream_id, result_msg)
            return True, result_msg, 1
        except Exception as exc:
            self.ctx.logger.error("获取收藏失败: %s", exc)
            msg = MessageFormatter.format_error("network", str(exc))
            await self._send_text(stream_id, msg)
            return False, msg, 1


def create_plugin() -> JMCosmosMaiBotPlugin:
    return JMCosmosMaiBotPlugin()
