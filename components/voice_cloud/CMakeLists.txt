idf_component_register(SRCS "voice_cloud.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES transport_ws audio_fe audio_player codec_enc event_bus esp_timer)
