"""Constants for Wrist Assistant delta API integration."""

DOMAIN = "wrist_assistant"
DATA_COORDINATOR = "delta_coordinator"
DATA_PAIRING_COORDINATOR = "pairing_coordinator"
DATA_CAMERA_STREAM_COORDINATOR = "camera_stream_coordinator"
DATA_NOTIFICATION_TOKEN_STORE = "notification_token_store"
DATA_APNS_CONFIG_STORE = "apns_config_store"
DATA_APNS_CLIENT = "apns_client"
PLATFORMS = ["sensor", "binary_sensor", "text"]
SERVICE_FORCE_RESYNC = "force_resync"
SERVICE_CREATE_PAIRING_CODE = "create_pairing_code"
SERVICE_SEND_NOTIFICATION = "send_notification"
NOTIFICATION_TOKEN_STORAGE_KEY = "wrist_assistant.notification_tokens"
NOTIFICATION_TOKEN_STORAGE_VERSION = 1
APNS_CONFIG_STORAGE_KEY = "wrist_assistant.apns_config"
APNS_CONFIG_STORAGE_VERSION = 1
