idf_component_register(SRCS "voice_session.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES audio_fe audio_player prompt_store event_bus feedback voice_cloud esp_timer)
