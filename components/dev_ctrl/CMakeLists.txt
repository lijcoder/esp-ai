idf_component_register(SRCS "dev_ctrl.c" "ws2812_led.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES board driver esp_timer event_bus audio_player)
