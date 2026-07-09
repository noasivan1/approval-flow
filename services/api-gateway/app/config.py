import os


class Settings:
    def __init__(self) -> None:
        self.service_name = os.getenv("SERVICE_NAME", "api-gateway")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.port = int(os.getenv("PORT", "8000"))


settings = Settings()
