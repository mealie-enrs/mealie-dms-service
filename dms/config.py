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
    swift_user_uploads_container: str = "proj26-user-uploads"
    swift_training_container: str = "proj26-training-data"


settings = Settings()
