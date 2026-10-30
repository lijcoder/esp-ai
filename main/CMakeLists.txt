idf_component_register(SRCS "app_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES board audio_io audio_fe audio_player assets diag config_store event_bus net_mgr)
