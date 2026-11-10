idf_component_register(SRCS "music_cloud.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES transport_ws audio_player event_bus esp_timer)
