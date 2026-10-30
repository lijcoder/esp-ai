idf_component_register(SRCS "diag.c"
                       INCLUDE_DIRS "include"
                       REQUIRES console esp_app_format esp_timer
                       PRIV_REQUIRES audio_fe config_store event_bus net_mgr nvs_flash)
