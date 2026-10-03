"""Application settings.

All secrets come from environment variables (never from Git). Feature flags
defined here default to disabled; env overrides use the ``FLAG_`` prefix,
e.g. ``FLAG_LIFECYCLE_AUTOMATION_ENABLED=1`` forces the flag on.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PVG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- runtime ---
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8350
    log_level: str = "INFO"

    # --- database ---
    database_url: str = "sqlite+pysqlite:///./pv_growth_dev.db"

    # --- security ---
    admin_token: str = ""  # when empty the admin API refuses every request
    telegram_webhook_secret: str = ""

    # --- telegram ---
    telegram_bot_token: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    free_channel_id: str = ""
    official_channel_id: str = ""

    # --- mirza (source of truth; read-only adapter) ---
    mirza_base_url: str = ""
    mirza_token: str = ""
    mirza_timeout_seconds: float = 10.0
    # read-only MySQL integration (hajsaman) via GrowthOS-owned forwarder
    mirza_mysql_host: str = ""
    mirza_mysql_port: int = 3306
    mirza_mysql_user: str = "pv_growth_ro"
    mirza_mysql_password: str = ""

    # --- PV provisioning (exclusive free configs) ---
    provisioning_base_url: str = ""
    provisioning_token: str = ""
    provisioning_timeout_seconds: float = 10.0
    provisioning_inbound_ids: str = "1"
    provisioning_sublink: str = ""
    free_daily_budget: int = 25

    # --- scheduler / jobs (enable in production env when Phase 3 lands) ---
    scheduler_enabled: bool = False
    telegram_polling_enabled: bool = True  # long-poll when no public webhook
    scheduler_interval_seconds: float = 15.0
    job_batch_size: int = 10
    job_lock_stale_seconds: int = 300
    job_max_attempts_default: int = 5

    # --- free-config pipeline limits ---
    fetch_max_configs: int = 2000
    health_check_max: int = 5
    health_check_concurrency: int = 3
    health_check_timeout_seconds: float = 4.0
    publish_top_n: int = 2

    # --- feature flags (all default OFF; see core/flags.py) ---
    flag_attribution_enabled: bool = False
    flag_free_config_enabled: bool = False
    flag_public_config_enabled: bool = False
    flag_pv_exclusive_config_enabled: bool = False
    flag_lifecycle_automation_enabled: bool = False
    flag_referral_enabled: bool = False
    flag_partner_enabled: bool = False
    flag_content_engine_enabled: bool = False
    flag_competitor_watch_enabled: bool = False
    flag_experiments_enabled: bool = False

    # --- preflight / smoke (paths and targets on the production host) ---
    protected_paths: str = (
        "/var/www/html/mirzaprobotconfig,/var/www/mirza_pro,/opt/pv-reseller,/opt/akhbot/app"
    )
    protected_containers: str = "pv-reseller-dashboard,akhbot-app"
    growth_port: int = 8350
    growth_subnet: str = "172.23.77.0/24"
    growth_container_name: str = "pv-growth-app"
    min_free_disk_mb: int = 2048
    min_free_mem_mb: int = 512
    smoke_mirza_url: str = "https://robot.ahsg.top"
    smoke_reseller_url: str = "https://npanel.softarg.ir"
    smoke_akh_url: str = "https://rasteh.softarg.ir"
    smoke_apache_url: str = "http://127.0.0.1:80"
    smoke_xui_url: str = "https://127.0.0.1:2083"
    smoke_pg_host: str = "127.0.0.1"
    smoke_pg_port: int = 5432
    smoke_growth_url: str = "http://127.0.0.1:8350/health"
    smoke_timeout_seconds: float = 8.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
