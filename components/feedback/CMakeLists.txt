idf_component_register(SRCS "feedback.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES audio_fe audio_player event_bus prompt_store esp_timer)
