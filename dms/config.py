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
    inference_model_name: str = "resnet50-pretrained"
    inference_image_size: int = 224
    inference_top_k_default: int = 5
    inference_top_k_max: int = 25
    inference_manifest_key: str = "recipe1m_versions/v1/manifest.parquet"
    inference_index_key: str = "recipe1m_versions/v1/feature_index.npz"
    inference_index_meta_key: str = "recipe1m_versions/v1/feature_index_meta.json"

    kaggle_username: str | None = None
    kaggle_key: str | None = None
    kaggle_download_dir: str = "/tmp/dms-kaggle-downloads"

    # Qdrant vector store
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333
    qdrant_collection: str = "recipe_features"

    # Prefect
    prefect_api_url: str = "http://prefect-server:4200/api"

    # dbt
    dbt_project_dir: str = "/app/transforms"


settings = Settings()
