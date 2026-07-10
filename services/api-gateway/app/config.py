import os


class Settings:
    def __init__(self) -> None:
        self.service_name = os.getenv("SERVICE_NAME", "api-gateway")
        self.log_level = os.getenv("LOG_LEVEL", "INFO")
        self.port = int(os.getenv("PORT", "8000"))
        # default goes through the local Dapr sidecar
        self.workflow_service_url = os.getenv(
            "WORKFLOW_SERVICE_URL",
            "http://localhost:3500/v1.0/invoke/workflow-service/method",
        )
        self.allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")


settings = Settings()
