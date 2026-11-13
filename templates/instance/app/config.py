"""What the app reads from its environment (`.env`, see `.env.example`)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://app:app@localhost:5432/app"
    # Meta app secret: verifies the X-Hub-Signature-256 header on webhook POSTs.
    meta_app_secret: str = ""
    # Our own random token, entered in the Meta webhook settings; checked on the GET handshake.
    whatsapp_verify_token: str = ""
    # Cloud API sender: a permanent system user token.
    whatsapp_access_token: str = ""
    # The WhatsApp Business Account: our templates' state is read from it at startup.
    whatsapp_business_account_id: str = ""
    graph_api_version: str = "v25.0"
