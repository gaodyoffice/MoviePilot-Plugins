import os
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.media_server import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from apscheduler.schedulers.background import BackgroundScheduler
from app.utils.http import RequestUtils


class StrmEmbySync(_PluginBase):
    # 插件基本信息
    plugin_name = "STRM-Emby同步"
    plugin_desc = "媒体转移完成后自动生成STRM文件并触发Emby刷新。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/refresh.png"
    plugin_version = "1.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"

    # 插件配置项ID前缀
    plugin_config_prefix = "strm_emby_sync_"

    # 加载顺序
    plugin_order = 50

    # 可见权限级别
    auth_level = 1

    # 私有属性
    _scheduler: Optional[BackgroundScheduler] = None
    _enabled = False
    _strm_path = ""
    _strm_content_type = "local"
    _strm_custom_prefix = ""
    _auto_refresh_emby = False
    _emby_refresh_scope = "library"
    _mediaserver_helper = None
    _emby_host = None
    _emby_apikey = None

    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self._mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._strm_path = config.get("strm_path", "")
            self._strm_content_type = config.get("strm_content_type", "local")
            self._strm_custom_prefix = config.get("strm_custom_prefix", "")
            self._auto_refresh_emby = config.get("auto_refresh_emby", False)
            self._emby_refresh_scope = config.get("emby_refresh_scope", "library")

    def get_state(self) -> bool:
        return self._enabled

    @eventmanager.register(EventType.TransferComplete)
    def handle_transfer_complete(self, event: Event):
        """监听转移完成事件，生成STRM并刷新Emby"""
        if not self._enabled:
            return

        event_data = event.event_data or {}
        transferinfo = event_data.get("transferinfo")
        if not transferinfo:
            return

        # 获取转移后的目标路径
        target_path = getattr(transferinfo.target_item, "path", None) if transferinfo.target_item else None
        if not target_path:
            logger.warn("STRM-Emby同步：未找到转移目标路径，跳过")
            return

        target_path = Path(target_path)
        if not target_path.exists():
            logger.warn(f"STRM-Emby同步：目标路径不存在 {target_path}")
            return

        # 生成STRM文件
        strm_path = self._generate_strm(target_path)
        if not strm_path:
            return

        # 触发Emby刷新
        if self._auto_refresh_emby:
            self._refresh_emby(target_path if self._emby_refresh_scope == "path" else None)

        self.post_message(
            title="STRM-Emby同步完成",
            message=f"已生成 {strm_path.name}，Emby刷新已触发" if self._auto_refresh_emby else f"已生成 {strm_path.name}"
        )

    def _generate_strm(self, media_path: Path) -> Optional[Path]:
        """生成STRM文件"""
        try:
            # 确定STRM保存目录
            if self._strm_path and Path(self._strm_path).exists():
                save_dir = Path(self._strm_path)
            else:
                save_dir = media_path.parent

            # 确保保存目录存在
            save_dir.mkdir(parents=True, exist_ok=True)

            # STRM文件名（与媒体文件同名，后缀.strm）
            strm_file = save_dir / f"{media_path.stem}.strm"

            # STRM文件内容
            if self._strm_content_type == "local":
                content = str(media_path.absolute())
            else:
                prefix = self._strm_custom_prefix.rstrip("/")
                # 尝试相对于媒体库路径生成相对路径
                if settings.MEDIA_PATH and media_path.is_relative_to(Path(settings.MEDIA_PATH)):
                    content = f"{prefix}/{media_path.relative_to(Path(settings.MEDIA_PATH))}"
                else:
                    content = f"{prefix}/{media_path.name}"

            # 写入STRM文件
            strm_file.write_text(content, encoding="utf-8")
            logger.info(f"STRM-Emby同步：已生成 {strm_file}")
            return strm_file

        except Exception as e:
            logger.error(f"STRM-Emby同步：生成STRM失败 - {str(e)}")
            return None

    def _get_emby_config(self) -> bool:
        """获取Emby服务配置"""
        try:
            server_services = self._mediaserver_helper.get_services(type_filter="emby")
            if not server_services:
                logger.warn("STRM-Emby同步：未找到Emby服务")
                return False

            # 取第一个Emby服务
            for server_name, server_info in server_services.items():
                config = server_info.config
                if not config:
                    continue
                self._emby_host = config.config.get("host", "")
                self._emby_apikey = config.config.get("apikey", "")
                if self._emby_host and self._emby_apikey:
                    if not self._emby_host.endswith("/"):
                        self._emby_host += "/"
                    if not self._emby_host.startswith("http"):
                        self._emby_host = "http://" + self._emby_host
                    logger.info(f"STRM-Emby同步：已获取Emby服务配置 {server_name}")
                    return True

            logger.warn("STRM-Emby同步：Emby服务配置不完整")
            return False

        except Exception as e:
            logger.error(f"STRM-Emby同步：获取Emby配置失败 - {str(e)}")
            return False

    def _refresh_emby(self, path: Optional[Path] = None):
        """触发Emby刷新"""
        if not self._get_emby_config():
            return

        try:
            if path:
                # 刷新指定路径
                logger.info(f"STRM-Emby同步：尝试刷新路径 {path}")
                self._refresh_emby_by_path(path)
            else:
                # 刷新整个媒体库
                logger.info("STRM-Emby同步：刷新Emby整个媒体库")
                self._refresh_emby_library()

        except Exception as e:
            logger.error(f"STRM-Emby同步：刷新Emby失败 - {str(e)}")

    def _refresh_emby_library(self):
        """刷新整个Emby媒体库"""
        try:
            # Emby API：刷新整个媒体库
            req_url = f"{self._emby_host}emby/Library/Refresh?api_key={self._emby_apikey}"
            with RequestUtils().post_res(req_url) as res:
                if res and res.status_code in [200, 204, 202]:
                    logger.info("STRM-Emby同步：已触发Emby整个媒体库刷新")
                else:
                    logger.error(f"STRM-Emby同步：刷新Emby媒体库失败，状态码 {res.status_code if res else '无响应'}")
        except Exception as e:
            logger.error(f"STRM-Emby同步：刷新Emby媒体库异常 - {str(e)}")

    def _refresh_emby_by_path(self, path: Path):
        """刷新指定路径（通过搜索父目录）"""
        try:
            # 搜索路径对应的Emby目录
            # 先尝试搜索父目录名称
            search_url = f"{self._emby_host}emby/Items?SearchTerm={path.parent.name}&IncludeItemTypes=Folder,Directory&Recursive=true&api_key={self._emby_apikey}"
            with RequestUtils().get_res(search_url) as res:
                if res and res.status_code == 200:
                    items = res.json().get("Items", [])
                    for item in items:
                        # 检查路径是否匹配（Emby中的Path字段）
                        if item.get("Path") and Path(item.get("Path")).resolve() == path.parent.resolve():
                            # 找到匹配的目录，刷新该目录
                            item_id = item.get("Id")
                            req_url = f"{self._emby_host}emby/Items/{item_id}/Refresh?Recursive=true&MetadataRefreshMode=FullRefresh&api_key={self._emby_apikey}"
                            with RequestUtils().post_res(req_url) as refresh_res:
                                if refresh_res and refresh_res.status_code in [200, 204, 202]:
                                    logger.info(f"STRM-Emby同步：已刷新Emby目录 {item.get('Name')}")
                                else:
                                    logger.warn(f"STRM-Emby同步：刷新目录失败 {item.get('Name')}")
                            return

            # 如果没找到具体目录，回退到刷新整个库
            logger.warn("STRM-Emby同步：未找到路径对应的Emby目录，回退到刷新整个库")
            self._refresh_emby_library()

        except Exception as e:
            logger.error(f"STRM-Emby同步：刷新指定路径异常 - {str(e)}")
            # 回退到刷新整个库
            self._refresh_emby_library()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页"""
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'strm_path',
                                            'label': 'STRM保存路径（可选）',
                                            'placeholder': '留空则与媒体文件同目录',
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'strm_content_type',
                                            'label': 'STRM内容类型',
                                            'items': [
                                                {'title': '本地路径', 'value': 'local'},
                                                {'title': '自定义链接前缀', 'value': 'custom'},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'strm_custom_prefix',
                                            'label': '自定义链接前缀',
                                            'placeholder': 'https://dlink.cloud',
                                            'show': 'strm_content_type === "custom"',
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_refresh_emby',
                                            'label': '自动刷新Emby',
                                        },
                                    }
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'emby_refresh_scope',
                                            'label': 'Emby刷新范围',
                                            'items': [
                                                {'title': '整个媒体库', 'value': 'library'},
                                                {'title': '仅刷新当前路径', 'value': 'path'},
                                            ],
                                            'show': 'auto_refresh_emby',
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "strm_path": "",
            "strm_content_type": "local",
            "strm_custom_prefix": "",
            "auto_refresh_emby": False,
            "emby_refresh_scope": "library",
        }

    def get_api(self) -> List[Dict[str, Any]]:
        """插件API"""
        return []

    def stop_service(self):
        """停止服务"""
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
