from pydantic_settings import BaseSettings
from pydantic import Field
from pathlib import Path
from dataclasses import dataclass, field as dc_field
from safelog import warn


class LLMSettings(BaseSettings):
    provider: str = "deepseek"
    fallback_chain: list[str] = Field(default=["deepseek", "openai", "ollama"])
    timeout_seconds: int = 30
    max_retries: int = 2
    daily_token_budget: int = 500_000
    per_agent_budget: int = 100_000
    #: WP1: TOML config path (used by llm.config.load_llm_config).
    llm_config_path: str = "configs/llm.toml"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"

    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_vision_model: str = "qwen-vl-max"

    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"

    model_config = {"env_file": ".env", "extra": "ignore"}


class EmbeddingSettings(BaseSettings):
    provider: str = "siliconflow"
    model: str = "BAAI/bge-m3"
    dim: int = 1024
    batch_size: int = 32
    cache_enabled: bool = True

    model_config = {"env_prefix": "EMBEDDING_", "env_file": ".env", "extra": "ignore"}


class VectorSettings(BaseSettings):
    persist_dir: str = ""
    migration_batch_size: int = 100

    model_config = {"env_prefix": "VECTOR_", "env_file": ".env", "extra": "ignore"}


class WeChatSettings(BaseSettings):
    key: str = ""
    db_path: str = ""
    version: int = 4
    self_wxid: str = ""
    owner_wxids: str = ""
    yinyue_wxid: str = ""
    image_aes_key: str = ""
    send_enabled: bool = False
    send_min_interval: float = 2.0
    accounts_json: str = ""

    model_config = {"env_prefix": "WECHAT_", "env_file": ".env", "extra": "ignore"}

    def get_self_wxid(self) -> str:
        if self.self_wxid:
            return self.self_wxid
        if self.db_path:
            import re
            m = re.search(r"(wxid_[a-zA-Z0-9]+)", self.db_path)
            if m:
                return m.group(1)
        return ""

    def get_owner_wxids(self) -> list[str]:
        if self.owner_wxids:
            return [w.strip() for w in self.owner_wxids.split(",") if w.strip()]
        return []

    def get_extra_accounts(self) -> list[dict]:
        if not self.accounts_json:
            return []
        import json
        try:
            data = json.loads(self.accounts_json)
            if isinstance(data, list):
                return data
        except Exception:
            warn("core/config/settings.py:92 Exception", err=repr(__import__("sys").exc_info()[1]))
        return []


class SyncSettings(BaseSettings):
    interval_minutes: int = 30

    model_config = {"env_prefix": "SYNC_", "env_file": ".env", "extra": "ignore"}


class AgentSettings(BaseSettings):
    # Track B5 removed the legacy analysis agents; analysis is now the
    # event-sourced worker in core.analysis.  The key remains for config
    # compatibility and status reporting.
    enabled_agents: list[str] = Field(default=[])
    auto_wire_events: bool = True

    model_config = {"env_file": ".env", "extra": "ignore"}


class UserProfileSettings(BaseSettings):
    keywords: list[str] = Field(default=[])
    projects: list[str] = Field(default=[])
    frequent_contacts: list[str] = Field(default=[])
    ignore_groups: list[str] = Field(default=[])
    classifier_enabled: bool = True
    classifier_min_relevance: float = 0.1
    classifier_urgent_keywords: list[str] = Field(default=[])
    classifier_task_keywords: list[str] = Field(default=[])

    model_config = {"env_prefix": "USER_", "env_file": ".env", "extra": "ignore"}


class PersonaSettings(BaseSettings):
    enabled: bool = False
    default_persona_id: int = 1
    memory_max_age_days: int = 90
    memory_decay_rate: float = 0.01
    reflection_interval_hours: int = 24
    proactive_cooldown_minutes: int = 60

    model_config = {"env_prefix": "PERSONA_", "env_file": ".env", "extra": "ignore"}


class DreamSettings(BaseSettings):
    enabled: bool = True
    idle_timeout_minutes: int = 5
    periodic_interval_minutes: int = 30
    auto_merge_threshold: float = 0.85
    auto_split_threshold: float = 0.9
    auto_adopt_orphans: bool = True
    auto_upgrade_summaries: bool = True
    auto_rebalance_budgets: bool = True

    model_config = {"env_prefix": "DREAM_", "env_file": ".env", "extra": "ignore"}


class SearchSettings(BaseSettings):
    tavily_api_key: str = ""
    bing_api_key: str = ""
    default_engine: str = "tavily"
    # DeepSeek 自研搜索（/v1/tools/search）：按请求计费，count 不影响价格 → 默认多要本地裁。
    deepseek_count: int = 10
    deepseek_need_content: bool = True
    deepseek_timeout_seconds: float = 20.0
    # /tools/fetch 单独开关：search 的 A/B 已证优，fetch 还没量 → 默认只换 search。
    deepseek_fetch_enabled: bool = False
    max_results: int = 5
    fetch_top_content: bool = True
    parallel_queries: bool = True
    cache_ttl_minutes: int = 10
    knowledge_auto_save: bool = True
    knowledge_max_docs: int = 100000
    knowledge_stale_days: int = 180
    # search_messages 默认往前搜索的小时数；0 或负数表示不限时间。
    default_hours_back: int = 24

    model_config = {"env_prefix": "SEARCH_", "env_file": ".env", "extra": "ignore"}


class WeatherSettings(BaseSettings):
    provider: str = "openweather"
    api_key: str = ""
    default_city: str = "上海"
    lang: str = "zh_cn"
    model_config = {"env_prefix": "WEATHER_", "env_file": ".env", "extra": "ignore"}


class TemporalSettings(BaseSettings):
    """时间感知（time awareness）配置。

    timezone 为空时使用系统本地时区；填写 IANA 名称（如 Asia/Shanghai）
    后，所有展示给模型/用户的时间都会换算到该时区。
    """

    timezone: str = ""
    history_relative_days: int = 7
    stale_message_hours: int = 48
    quiet_hours_start: int = 23
    quiet_hours_end: int = 7

    model_config = {"env_prefix": "TEMPORAL_", "env_file": ".env", "extra": "ignore"}


class PathSettings(BaseSettings):
    data_dir: Path = Path("./data")
    decrypted_dir: Path = Path("./data/decrypted")
    output_dir: Path = Path("./output")
    log_dir: Path = Path("./logs")
    platforms_dir: Path = Path("./data/platforms")
    meta_db_path: Path = Path("./data/yinyue_meta.db")

    model_config = {"env_file": ".env", "extra": "ignore"}

    def get_account_dir(self, platform: str, account_id: str) -> Path:
        return self.platforms_dir / platform / account_id

    def get_account_db_path(self, platform: str, account_id: str) -> Path:
        return self.get_account_dir(platform, account_id) / "account.db"

    def get_account_emoji_dir(self, platform: str, account_id: str) -> Path:
        return self.get_account_dir(platform, account_id) / "emoji"

    def get_account_media_dir(self, platform: str, account_id: str) -> Path:
        return self.get_account_dir(platform, account_id) / "media"

    def get_account_decrypted_dir(self, platform: str, account_id: str) -> Path:
        return self.decrypted_dir / platform / account_id


@dataclass
class AccountConfig:
    account_id: str
    platform: str
    role: str = "yinyue"
    db_path: str = ""
    emoji_dir: str = ""
    media_dir: str = ""
    decrypted_dir: str = ""
    send_enabled: bool = False
    wx_key: str = ""
    wx_db_path: str = ""
    wx_version: int = 4
    wx_image_aes_key: str = ""
    wx_send_min_interval: float = 2.0
    owner_wxids: list[str] = dc_field(default_factory=list)
    # Platform-specific config (tokens, cookies, server URLs, ...).  Keeping
    # it generic lets new platforms be added without growing this dataclass.
    extra: dict = dc_field(default_factory=dict)

    def resolve_paths(self, paths: PathSettings) -> None:
        if not self.db_path:
            self.db_path = str(paths.get_account_db_path(self.platform, self.account_id))
        if not self.emoji_dir:
            self.emoji_dir = str(paths.get_account_emoji_dir(self.platform, self.account_id))
        if not self.media_dir:
            self.media_dir = str(paths.get_account_media_dir(self.platform, self.account_id))
        if not self.decrypted_dir:
            self.decrypted_dir = str(paths.get_account_decrypted_dir(self.platform, self.account_id))


@dataclass
class PlatformConfig:
    key: str
    display: str = ""
    accounts: list[AccountConfig] = dc_field(default_factory=list)

    @property
    def yinyue_account(self) -> AccountConfig | None:
        return next((a for a in self.accounts if a.role == "yinyue"), None)

    @property
    def owner_accounts(self) -> list[AccountConfig]:
        return [a for a in self.accounts if a.role == "owner"]


class PlatformAccountSettings(BaseSettings):
    wechat_account: str = ""
    wechat_owner_accounts: str = ""
    bilibili_account: str = ""
    bilibili_owner_accounts: str = ""
    douyin_account: str = ""
    douyin_owner_accounts: str = ""
    qq_account: str = ""
    qq_owner_accounts: str = ""
    telegram_account: str = ""
    telegram_owner_accounts: str = ""
    # Generic escape hatch for arbitrary platforms/accounts:
    # PLATFORMS_CONFIGS_JSON='[{"platform":"telegram","account_id":"123",
    #   "role":"yinyue","token":"..."}]'
    configs_json: str = ""

    model_config = {"env_prefix": "PLATFORMS_", "env_file": ".env", "extra": "ignore"}

    def get_generic_configs(self) -> list[dict]:
        if not self.configs_json:
            return []
        import json
        try:
            data = json.loads(self.configs_json)
        except Exception:
            warn("core/config/settings.py:303 Exception", err=repr(__import__("sys").exc_info()[1]))
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            # Also accept {"platform": [account, ...]}.
            out = []
            for platform, accounts in data.items():
                if isinstance(accounts, list):
                    for account in accounts:
                        if isinstance(account, dict):
                            merged = dict(account)
                            merged.setdefault("platform", platform)
                            out.append(merged)
            return out
        return []

    def get_accounts(self) -> dict[str, str]:
        result = {}
        if self.wechat_account:
            result["wechat"] = self.wechat_account
        if self.bilibili_account:
            result["bilibili"] = self.bilibili_account
        if self.douyin_account:
            result["douyin"] = self.douyin_account
        if self.qq_account:
            result["qq"] = self.qq_account
        if self.telegram_account:
            result["telegram"] = self.telegram_account
        return result

    def get_owner_accounts(self) -> dict[str, list[str]]:
        result = {}
        if self.wechat_owner_accounts:
            result["wechat"] = [x.strip() for x in self.wechat_owner_accounts.split(",") if x.strip()]
        if self.bilibili_owner_accounts:
            result["bilibili"] = [x.strip() for x in self.bilibili_owner_accounts.split(",") if x.strip()]
        if self.douyin_owner_accounts:
            result["douyin"] = [x.strip() for x in self.douyin_owner_accounts.split(",") if x.strip()]
        if self.qq_owner_accounts:
            result["qq"] = [x.strip() for x in self.qq_owner_accounts.split(",") if x.strip()]
        if self.telegram_owner_accounts:
            result["telegram"] = [x.strip() for x in self.telegram_owner_accounts.split(",") if x.strip()]
        return result


class AppConfig(BaseSettings):
    llm: LLMSettings = LLMSettings()
    embedding: EmbeddingSettings = EmbeddingSettings()
    vector: VectorSettings = VectorSettings()
    wechat: WeChatSettings = WeChatSettings()
    sync: SyncSettings = SyncSettings()
    agents: AgentSettings = AgentSettings()
    user_profile: UserProfileSettings = UserProfileSettings()
    persona: PersonaSettings = PersonaSettings()
    temporal: TemporalSettings = TemporalSettings()
    dream: DreamSettings = DreamSettings()
    weather: WeatherSettings = WeatherSettings()
    search: SearchSettings = SearchSettings()
    paths: PathSettings = PathSettings()
    platforms: PlatformAccountSettings = PlatformAccountSettings()
    platform_configs: dict[str, PlatformConfig] = Field(default={}, alias="platform_configs")

    model_config = {"env_file": ".env", "extra": "ignore"}

    def get_all_accounts(self) -> dict[str, str]:
        accounts = self.platforms.get_accounts()
        if "wechat" not in accounts and self.wechat.yinyue_wxid:
            accounts["wechat"] = self.wechat.yinyue_wxid
        # 通用配置（PLATFORMS_CONFIGS_JSON）里的鲸鲸号也要进映射：路由取机器人号
        # 做名字提及判定、纠错认 owner 都靠它（批次 4 合并两套配置）。
        for key, platform_config in self.build_platform_configs().items():
            bot = platform_config.yinyue_account
            if bot and bot.account_id and key not in accounts:
                accounts[key] = bot.account_id
        return accounts

    def get_all_owner_accounts(self) -> dict[str, list[str]]:
        owner_accounts = self.platforms.get_owner_accounts()
        if "wechat" not in owner_accounts:
            owner_wxids = self.wechat.get_owner_wxids()
            if owner_wxids:
                owner_accounts["wechat"] = owner_wxids
        for key, platform_config in self.build_platform_configs().items():
            known = owner_accounts.setdefault(key, [])
            for owner in platform_config.owner_accounts:
                if owner.account_id and owner.account_id not in known:
                    known.append(owner.account_id)
        return owner_accounts

    def build_platform_configs(self) -> dict[str, PlatformConfig]:
        if self.platform_configs:
            return self.platform_configs

        from core.execution.platform_registry import get_platform
        configs: dict[str, PlatformConfig] = {}

        def ensure_platform(platform: str) -> PlatformConfig:
            if platform not in configs:
                defn = get_platform(platform)
                configs[platform] = PlatformConfig(
                    key=platform,
                    display=(defn.display if defn else platform),
                    accounts=[],
                )
            return configs[platform]

        def add_account(ac: AccountConfig) -> None:
            pc = ensure_platform(ac.platform)
            if any(a.account_id == ac.account_id for a in pc.accounts):
                return
            ac.resolve_paths(self.paths)
            pc.accounts.append(ac)

        # 1) Generic, platform-agnostic config first.  This is the path new
        #    platforms/accounts should use; legacy env vars below are kept as
        #    compatibility shims and merged without duplicates.
        known = set(AccountConfig.__dataclass_fields__)
        for raw in self.platforms.get_generic_configs():
            platform = str(raw.get("platform") or raw.get("key") or "").strip()
            account_id = str(raw.get("account_id") or raw.get("id") or "").strip()
            if not platform or not account_id:
                continue
            kwargs = {k: v for k, v in raw.items() if k in known and k != "extra"}
            kwargs["platform"] = platform
            kwargs["account_id"] = account_id
            kwargs.setdefault("role", "yinyue")
            extra = dict(raw.get("extra") or {}) if isinstance(raw.get("extra"), dict) else {}
            for k, v in raw.items():
                if k not in known:
                    extra[k] = v
            kwargs["extra"] = extra
            try:
                add_account(AccountConfig(**kwargs))
            except Exception:
                # Ignore malformed generic entries; config validation/logging
                # in core.config.validate will surface the real problem.
                warn("core/config/settings.py:438 Exception", err=repr(__import__("sys").exc_info()[1]))
                continue

        # 2) Legacy WeChat env configuration.
        if self.wechat.yinyue_wxid or self.wechat.self_wxid:
            yinyue_id = self.wechat.yinyue_wxid or self.wechat.self_wxid
            extra_map = {}
            for ea in self.wechat.get_extra_accounts():
                wxid = ea.get("wxid", "")
                if wxid:
                    extra_map[wxid] = ea
            add_account(AccountConfig(
                account_id=yinyue_id,
                platform="wechat",
                role="yinyue",
                send_enabled=self.wechat.send_enabled,
                wx_key=self.wechat.key,
                wx_db_path=self.wechat.db_path,
                wx_version=self.wechat.version,
                wx_image_aes_key=self.wechat.image_aes_key,
                wx_send_min_interval=self.wechat.send_min_interval,
                owner_wxids=self.wechat.get_owner_wxids(),
            ))
            for oid in self.wechat.get_owner_wxids():
                ea = extra_map.get(oid, {})
                add_account(AccountConfig(
                    account_id=oid,
                    platform="wechat",
                    role="owner",
                    send_enabled=self.wechat.send_enabled,
                    wx_key=ea.get("key", ""),
                    wx_db_path=ea.get("db_path", ""),
                    wx_version=ea.get("version", self.wechat.version) if ea.get("db_path") else 0,
                    wx_image_aes_key=ea.get("image_aes_key", ""),
                    wx_send_min_interval=self.wechat.send_min_interval,
                ))

        # 3) Legacy per-platform env vars.
        legacy_display = {"qq": "QQ", "telegram": "Telegram"}
        for plat_key, plat_display in legacy_display.items():
            plat_id = self.platforms.get_accounts().get(plat_key, "")
            if plat_id:
                add_account(AccountConfig(
                    account_id=plat_id,
                    platform=plat_key,
                    role="yinyue",
                    owner_wxids=list(self.platforms.get_owner_accounts().get(plat_key, [])),
                ))
            for oid in self.platforms.get_owner_accounts().get(plat_key, []):
                add_account(AccountConfig(account_id=oid, platform=plat_key, role="owner"))

        self.platform_configs = configs
        return configs

    def get_platform_config(self, platform: str) -> PlatformConfig | None:
        return self.build_platform_configs().get(platform)

    def get_account_config(self, platform: str, account_id: str) -> AccountConfig | None:
        pc = self.get_platform_config(platform)
        if not pc:
            return None
        return next((a for a in pc.accounts if a.account_id == account_id), None)

    def build_account_config(self, platform: str, account_id: str, role: str = "yinyue") -> AccountConfig:
        existing = self.get_account_config(platform, account_id)
        if existing:
            return existing
        ac = AccountConfig(
            account_id=account_id,
            platform=platform,
            role=role,
        )
        if platform == "wechat":
            ac.send_enabled = self.wechat.send_enabled
            ac.wx_key = self.wechat.key
            ac.wx_db_path = ""
            ac.wx_version = self.wechat.version
            ac.wx_image_aes_key = self.wechat.image_aes_key
            ac.wx_send_min_interval = self.wechat.send_min_interval
        ac.resolve_paths(self.paths)
        return ac

    def all_account_configs(self) -> list[AccountConfig]:
        result = []
        for pc in self.build_platform_configs().values():
            result.extend(pc.accounts)
        return result


config = AppConfig()
