idf_component_register(SRCS "dev_ctrl.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES board driver esp_timer event_bus audio_player)
