idf_component_register(SRCS "transport_ws.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_websocket_client esp_event esp_timer event_bus net_mgr mbedtls)
