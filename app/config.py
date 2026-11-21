from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://hackathon:hackathon@localhost:5432/hackathon"
    auth_mode: str = "mock"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
