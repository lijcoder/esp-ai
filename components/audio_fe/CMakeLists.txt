idf_component_register(SRCS "audio_fe.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES audio_io esp_timer event_bus espressif__esp-sr)
