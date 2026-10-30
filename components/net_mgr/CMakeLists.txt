idf_component_register(SRCS "net_mgr.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_wifi esp_netif esp_event esp_timer event_bus)
