idf_component_register(SRCS "music_session.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES music_cloud audio_player event_bus esp_timer)
