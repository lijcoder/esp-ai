idf_component_register(SRCS "diag.c"
                       INCLUDE_DIRS "include"
                       REQUIRES console esp_app_format esp_timer
                       PRIV_REQUIRES audio_fe audio_io audio_player codec_enc config_store dev_ctrl event_bus feedback music_cloud music_session net_mgr prompt_store nvs_flash transport_ws voice_cloud voice_session espressif__esp-sr)
