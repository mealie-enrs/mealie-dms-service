from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "DMS"
    role: str = "api"

    database_url: str = "postgresql+psycopg://dms:dms@localhost:5432/dms"
    redis_url: str = "redis://localhost:6379/0"

    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    swift_auth_url: str | None = None
    swift_username: str | None = None
    swift_password: str | None = None
    swift_project_name: str | None = None
    swift_user_domain_name: str = "default"
    swift_project_domain_name: str = "default"
    swift_app_credential_id: str | None = None
    swift_app_credential_secret: str | None = None
    swift_user_uploads_container: str = "proj26-user-uploads"
    swift_training_container: str = "proj26-training-data"
    swift_recipe1m_prefix: str = "recipe1m"

    kaggle_username: str | None = None
    kaggle_key: str | None = None
    kaggle_download_dir: str = "/tmp/dms-kaggle-downloads"


settings = Settings()
