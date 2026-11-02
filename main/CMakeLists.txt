idf_component_register(SRCS "app_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES board audio_io audio_fe audio_player assets diag config_store dev_ctrl event_bus feedback net_mgr transport_ws voice_cloud voice_session)
