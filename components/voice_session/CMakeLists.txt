idf_component_register(SRCS "voice_session.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES audio_fe audio_io audio_player assets event_bus voice_cloud esp_timer)
